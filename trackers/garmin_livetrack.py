import asyncio
import aiohttp
import datetime
import re

import trackers

# https://www.cloudmailin.com/

async def config(app, settings):
    app['trackers.garmin_livetrack.session'] = garmin_livetrack_session = aiohttp.ClientSession()
    app['trackers.garmin_livetrack.config'] = await get_service_config(garmin_livetrack_session)
    return garmin_livetrack_session

async def start_event_tracker(app, settings, event_name, event_data, tracker_data):
    # TODO
    session_token_match = url_session_token_matcher(url).groupdict()
    tracker = trackers.Tracker('garmin_livetrack.{}'.format(session_token_match['session']))
    monitor_task = asyncio.ensure_future(monitor_session(
        app['trackers.garmin_livetrack.session'], app['trackers.garmin_livetrack.config'], session_token_match, tracker))
    return tracker, monitor_task


async def get_service_config(client_session):
    async with client_session.get('http://livetrack.garmin.com/services/config') as resp:
        return await resp.json()


url_session_token_matcher = re.compile('http://livetrack.garmin.com/session/(?P<session>.*)/token/(?P<token>.*)').match



async def monitor_session(client_session, service_config, session_token_match, tracker):
    session_url = 'http://livetrack.garmin.com/services/session/{session}/token/{token}'.format_map(session_token_match)
    tracklog_url = 'http://livetrack.garmin.com/services/trackLog/{session}/token/{token}'.format_map(session_token_match)
    last_status = None

    async def monitor_status():
        nonlocal last_status
        while True:
            try:
                session = await (await client_session.get(session_url)).json()
                status = session['sessionStatus']
                if status != last_status:
                    time = datetime.datetime.fromtimestamp(session['endTime'] / 1000)
                    await tracker.new_points([{'time': time, 'status': status}])
                    last_status = status

                if status == 'Complete':
                    break
            except Exception:
                tracker.logger.exception('Error getting session:')
            await asyncio.sleep(service_config['sessionRefreshRate'] / 1000)

    monitor_status_task = asyncio.ensure_future(monitor_status())

    last_timestamp = 0
    while True:
        try:
            reqs = await client_session.get(tracklog_url, params=(('from', str(last_timestamp)), ), )
            tracklog = await (reqs).json()
            if tracklog:
                points = []
                for item in tracklog:
                    time = datetime.datetime.fromtimestamp(item['timestamp']/1000)
                    point = {'time': time}
                    if item['latitude'] != 0 and item['longitude'] != 0:  # Filter out null island
                        point['position'] = (item['latitude'], item['longitude'], float(item['metaData']['ELEVATION']))
                    # TODO hr, power cad
                    points.append(point)
                await tracker.new_points(points)
                last_timestamp = tracklog[-1]['timestamp']

        except Exception:
            tracker.logger.exception('Error getting tracklog:')

        if last_status == 'Complete':
            break
        await asyncio.sleep(service_config['tracklogRefreshRate']/1000)

    await monitor_status_task



async def main(url):

    async with aiohttp.ClientSession() as client_session:
        service_config = await get_service_config(client_session)
        tracker, monitor_task = await start_monitor_session(client_session, service_config, url)
        trackers.print_tracker(tracker)
        await monitor_task

if __name__ == "__main__":
    loop = asyncio.get_event_loop()
    loop.run_until_complete(main('http://livetrack.garmin.com/session/cad74921-29af-4fe9-99f2-896b5972fbed/token/84D3B791E6C43ED2179CB59FB37CA24'))
