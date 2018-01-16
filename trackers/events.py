import asyncio
import base64
import datetime
import hashlib
import logging
import os
from contextlib import closing
from functools import partial

import aionotify
import msgpack
import yaml

from trackers.analyse import AnalyseTracker, get_analyse_routes
from trackers.base import BlockedList, cancel_and_wait_task, Observable
from trackers.dulwich_helpers import TreeReader, TreeWriter
from trackers.general import index_and_hash_tracker, start_replay_tracker
from trackers.persisted_func_cache import PersistedFuncCache

logger = logging.getLogger(__name__)


async def load_events_with_watcher(app, ref=b'HEAD', **kwargs):
    try:
        await load_events(app, ref=ref, **kwargs)

        repo = app['trackers.data_repo']

        while True:
            refnames, sha = repo.refs.follow(ref)
            paths = [repo.refs.refpath(ref) for ref in refnames]
            logger.debug(f'Watching paths {paths}')

            try:
                with closing(aionotify.Watcher()) as watcher:
                    await watcher.setup(asyncio.get_event_loop())
                    for path in paths:
                        watcher.watch(path, flags=aionotify.Flags.MODIFY + aionotify.Flags.DELETE_SELF + aionotify.Flags.MOVE_SELF)
                    await watcher.get_event()
            except OSError as e:
                logger.error(e)
                break

            await asyncio.sleep(0.1)

            new_sha = repo.refs[ref]
            if sha != new_sha:
                logger.info('Ref {} changed {} -> {}. Reloading.'.format(ref.decode(), sha.decode()[:6], new_sha.decode()[:6]))
                await load_events(app, ref=ref, **kwargs)
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception('Error in load_events_with_watcher: ')


async def load_events(app, ref=b'HEAD', new_event_observable=Observable(logger), removed_event_observable=Observable(logger)):
    events = app.setdefault('trackers.events', {})
    try:
        tree_reader = TreeReader(app['trackers.data_repo'], treeish=ref)
    except KeyError:
        pass
    else:
        names = set(tree_reader.tree_items('events'))
        for name in events.keys() - names:
            await events[name].stop_and_complete_trackers()
            await removed_event_observable(events.pop(name))
        for name in names:
            if name in events:
                await events[name].reload(tree_reader)
            else:
                events[name] = event = await Event.load(app, name, tree_reader)
                await new_event_observable(event)


def hash_bytes(b):
    return base64.urlsafe_b64encode(hashlib.sha1(b).digest()).decode('ascii')


class Event(object):
    def __init__(self, app, name, config=None, routes=None):
        self.name = name
        self.app = app
        self.logger = logging.getLogger(f'event.{name}')
        self.rider_trackers = {}
        self.rider_trackers_blocked_list = {}

        self.trackers_started = False
        self.starting_fut = None

        self.config_routes_change_observable = Observable(self.logger)
        self.rider_new_points_observable = Observable(self.logger)
        self.rider_blocked_list_update_observable = Observable(self.logger)

        self.path = os.path.join('events', name)
        self.routes_path = os.path.join(self.path, 'routes')
        self.git_hash = None

        self.config_path = os.path.join(self.path, 'data.yaml')
        self.config = config
        self.config_hash = hash_bytes(yaml.dump(config).encode()) if config else None

        self.routes_path = os.path.join(self.path, 'routes')
        self.routes = routes
        self.routes_hash = hash_bytes(msgpack.dumps(routes)) if routes else None

    @classmethod
    async def load(cls, app, name, tree_reader):
        event = Event(app, name)
        await event._load(tree_reader)
        return event

    async def reload(self, tree_reader):
        _, git_hash = tree_reader.lookup(self.path)
        if self.git_hash != git_hash:
            was_started = self.trackers_started
            await self.stop_and_complete_trackers(call_tracker_change_callbacks=False)
            await self._load(tree_reader)
            if was_started:
                await self.start_trackers()

    async def _load(self, tree_reader):
        if self.starting_fut or self.trackers_started:
            raise Exception("Can't load while starting or started.")

        _, self.git_hash = tree_reader.lookup(self.path)
        config_bytes = tree_reader.get(self.config_path).data
        self.config = yaml.load(config_bytes.decode())
        self.config_hash = hash_bytes(config_bytes)

        if tree_reader.exists(self.routes_path):
            routes_bytes = tree_reader.get(self.routes_path).data
            self.routes = msgpack.loads(routes_bytes, encoding='utf8')
            self.routes_hash = hash_bytes(routes_bytes)
        else:
            self.routes = []
            self.routes_hash = None

        await self.config_routes_change_observable(self)

    def save(self, message, author=None, tree_writer=None):
        if tree_writer is None:
            tree_writer = TreeWriter(self.app['trackers.data_repo'])
        config_text = yaml.dump(self.config, default_flow_style=False)
        tree_writer.set_data(self.config_path, config_text.encode())

        if self.routes:
            routes_bytes = msgpack.dumps(self.routes)
            tree_writer.set_data(self.routes_path, routes_bytes)
        else:
            if tree_writer.exists(self.routes_path):
                tree_writer.remove(self.routes_path)
        tree_writer.commit(message, author=author)
        _, self.git_hash = tree_writer.lookup(self.path)

    async def start_trackers(self):
        if not self.trackers_started:
            self.starting_fut = asyncio.ensure_future(self._start_trackers())
            try:
                await self.starting_fut
            finally:
                self.starting_fut = None

    async def _start_trackers(self):
        self.logger.info('Starting {}'.format(self.name))

        analyse = self.config.get('analyse', False)
        replay = self.config.get('replay', False)
        is_live = self.config.get('live', False)
        event_start = self.config.get('event_start')

        if analyse:
            analyse_routes = get_analyse_routes(self.routes)
            find_closest_cache_dir = os.path.join(self.app['trackers.settings']['cache_path'], 'find_closest')
            os.makedirs(find_closest_cache_dir, exist_ok=True)
            if self.routes:
                find_closest_cache = PersistedFuncCache(os.path.join(find_closest_cache_dir, self.routes_hash))
            else:
                find_closest_cache = None

        if replay:
            replay_start = datetime.datetime.now() + datetime.timedelta(seconds=2)

        for rider in self.config['riders']:
            if rider['tracker']:
                start_tracker = self.app['start_event_trackers'][rider['tracker']['type']]
                tracker = await start_tracker(self.app, self, rider['name'], rider['tracker'])
                if replay:
                    tracker = await start_replay_tracker(tracker, event_start, replay_start)
                if analyse:
                    tracker = await AnalyseTracker.start(tracker, event_start, analyse_routes, find_closest_cache=find_closest_cache)
                tracker = await index_and_hash_tracker(tracker)

                await self.on_rider_new_points(rider['name'], tracker, tracker.points)
                tracker.new_points_observable.subscribe(partial(self.on_rider_new_points, rider['name']))

                self.rider_trackers[rider['name']] = tracker
                self.rider_trackers_blocked_list[rider['name']] = BlockedList.from_tracker(
                    tracker, entire_block=not is_live,
                    new_update_callbacks=(partial(self.rider_blocked_list_update_observable, self, rider['name']), ))
        self.trackers_started = True

    async def stop_and_complete_trackers(self, call_tracker_change_callbacks=True):
        if self.starting_fut:
            cancel_and_wait_task(self.starting_fut)

        for tracker in self.rider_trackers.values():
            tracker.stop()
        for tracker in self.rider_trackers.values():
            try:
                await tracker.complete()
            except Exception:
                self.logger.exception('Unhandled tracker error: ')
        self.rider_trackers = {}
        self.rider_trackers_blocked_list = {}
        self.trackers_started = False

    async def on_rider_new_points(self, rider_name, tracker, new_points):
        await self.rider_new_points_observable(self, rider_name, tracker, new_points)
