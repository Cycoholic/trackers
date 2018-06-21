import asyncio
import bisect
import collections
import copy
import datetime
import logging
import operator
from functools import partial
from itertools import chain
from operator import itemgetter

import attr
from numpy import (
    arccos,
    array_equal,
    cross,
    deg2rad,
    dot,
    rad2deg,
    seterr,
)
from numpy.linalg import norm
from nvector import (
    lat_lon2n_E,
    n_E2lat_lon,
    n_EB_E2p_EB_E,
    p_EB_E2n_EB_E,
    unit,
)

try:
    from nvector import interpolate
except ImportError:
    # Copy paste hack till this gets released: https://github.com/garyvdm/Nvector/pull/1
    from numpy import nan

    def interpolate(path, ti):
        """
        Return the interpolated point along the path

        Parameters
        ----------
        path: tuple of n-vectors (positionA, po)

        ti: real scalar
            interpolation time assuming position A and B is at t0=0 and t1=1,
            respectively.

        Returns
        -------
        point: Nvector
            point of interpolation along path
        """

        n_EB_E_t0, n_EB_E_t1 = path
        n_EB_E_ti = unit(n_EB_E_t0 + ti * (n_EB_E_t1 - n_EB_E_t0), norm_zero_vector=nan)
        return n_EB_E_ti


from trackers.base import Tracker

logger = logging.getLogger(__name__)

seterr(all='raise')


class AnalyseTracker(Tracker):

    @classmethod
    async def start(cls, org_tracker, analyse_start_time, routes, track_break_time=datetime.timedelta(minutes=15),
                    track_break_dist=10000, find_closest_cache=None):
        self = cls('analysed.{}'.format(org_tracker.name))
        self.org_tracker = org_tracker
        self.analyse_start_time = analyse_start_time
        self.routes = routes
        self.track_break_time = track_break_time
        self.track_break_dist = track_break_dist
        if find_closest_cache:
            find_closest_cache.func = find_closest_point_pair_routes
            find_closest_cache.key = find_closest_point_pair_routes_cache_key
            find_closest_cache.unpack = partial(find_closest_point_pair_routes_unpack, routes)
            find_closest_cache.pack = find_closest_point_pair_routes_pack
            self.find_closest = find_closest_cache
        else:
            self.find_closest = find_closest_point_pair_routes

        self.completed = asyncio.ensure_future(self._completed())

        self.off_route_tracker = Tracker(f'offroute.{self.name}', completed=self.completed)
        self.reset()

        await self.on_new_points(org_tracker, org_tracker.points)
        org_tracker.new_points_observable.subscribe(self.on_new_points)
        org_tracker.reset_points_observable.subscribe(self.on_reset_points)

        return self

    def reset(self):
        self.prev_point_with_position = None
        self.prev_point_with_position_point = None
        self.prev_unit_vector = None
        self.current_track_id = 0
        self.prev_closest = None
        self.prev_route_dist = 0
        self.prev_route_dist_time = None
        self.going_forward = None
        self.finished = False
        self.is_off_route = False
        self.off_route_track_id = 0
        self.total_dist = 0

    def stop(self):
        self.org_tracker.stop()

    async def _completed(self):
        await self.org_tracker.complete()

    async def on_reset_points(self, tracker):
        await self.reset_points()
        await self.off_route_tracker.reset_points()
        self.reset()

    async def on_new_points(self, tracker, new_points):
        self.logger.debug('analyse_tracker_new_points ({} points)'.format(len(new_points)))

        new_new_points = []
        new_off_route_points = []
        log_time = datetime.datetime.now()
        log_i = 0
        last_route_point = self.routes[0]['points'][-1] if self.routes else None

        last_point_i = len(new_points) - 1
        did_slow_log = False

        for i, point in enumerate(new_points):
            point = copy.deepcopy(point)
            point.pop('status', None)
            point.pop('dist_route', None)
            point.pop('track_id', None)
            if 'position' in point:
                point_point = Point(*point['position'][:2])

                if not self.finished and self.analyse_start_time and self.analyse_start_time <= point['time']:
                    closest = self.find_closest(
                        self.routes, point_point, 5000,
                        self.prev_closest.route_i if self.prev_closest else None,
                        250, self.prev_route_dist)
                    if closest and closest.dist > 100000:
                        closest = None

                    route_dist = None
                    if closest:
                        route_dist = route_distance(closest.route, closest)
                        point['dist_route'] = round(route_dist)
                        self.going_forward = route_dist > self.prev_route_dist
                        self.prev_route_dist = route_dist
                        self.prev_route_dist_time = point['time']
                        if 'elevation' in closest.route and closest.dist > 250:
                            point['route_elevation'] = round(route_elevation(closest.route, route_dist))
                        if closest.route_i == 0 and abs(route_dist - last_route_point.distance) < 100:
                            self.logger.debug('Finished')
                            self.finished = True
                            point['finished_time'] = point['time']
                            point['rider_status'] = 'Finished'
                    else:
                        self.going_forward = None
                        self.prev_route_dist_time = None

                    time = None
                    dist_from_last = None
                    if self.prev_point_with_position_point:
                        prev_point = self.prev_point_with_position
                        time = point['time'] - prev_point['time']
                        if 'server_time' in point and 'server_time' in prev_point:
                            point['server_time_from_last'] = point['server_time'] - prev_point['server_time']

                        if (
                                closest and closest.dist < 250 and
                                self.prev_closest and self.prev_closest.dist < 250 and
                                closest.route_i == self.prev_closest.route_i):
                            dist_from_last = abs(
                                route_distance_no_adjust(closest.route, closest) -
                                route_distance_no_adjust(self.prev_closest.route, self.prev_closest))
                        else:
                            dist_from_last = distance(point_point, self.prev_point_with_position_point)

                        if not array_equal(point_point.pv, self.prev_point_with_position_point.pv):
                            self.prev_unit_vector = unit(point_point.pv - self.prev_point_with_position_point.pv)
                        else:
                            self.prev_unit_vector = None
                    else:
                        # Assume the last point was at the start of the route, and at the start of then event.
                        self.prev_unit_vector = None
                        time = point['time'] - self.analyse_start_time
                        if route_dist is not None:
                            dist_from_last = route_dist

                    if time:
                        point['time_from_last'] = time

                    if dist_from_last:
                        self.total_dist += dist_from_last
                        point['dist'] = round(self.total_dist)
                        point['dist_from_last'] = round(dist_from_last)

                    if time and dist_from_last:
                        seconds = time.total_seconds()
                        if seconds != 0:
                            point['speed_from_last'] = round(dist_from_last / seconds * 3.6, 1)

                        if time > self.track_break_time and dist_from_last > self.track_break_dist:
                            self.current_track_id += 1
                            self.off_route_track_id += 1

                    # self.logger.info((not self.routes, not closest, closest.dist > 500 if closest else None, not self.going_forward and point.get('dist_from_last', 0)))
                    if not self.routes or not closest or closest.dist > 200 or (not self.going_forward and point.get('dist_from_last', 0) > 200):
                        # self.logger.info('off_route')
                        if not self.is_off_route and self.prev_point_with_position and self.prev_point_with_position['track_id'] == self.current_track_id:
                            new_off_route_points.append({'position': self.prev_point_with_position['position'], 'track_id': self.off_route_track_id})
                        self.is_off_route = True
                        new_off_route_points.append({'position': point['position'], 'track_id': self.off_route_track_id})
                    elif self.is_off_route:
                        new_off_route_points.append({'position': point['position'], 'track_id': self.off_route_track_id})
                        self.is_off_route = False
                        self.off_route_track_id += 1

                    self.prev_closest = closest
                else:
                    self.prev_closest = None
                    self.going_forward = None

                self.prev_point_with_position = point
                self.prev_point_with_position_point = point_point
                point['track_id'] = self.current_track_id
            new_new_points.append(point)

            is_last_point = i == last_point_i
            if i % 10 == 9 or is_last_point:
                now = datetime.datetime.now()
                log_time_delta = (now - log_time).total_seconds()
                if log_time_delta >= 1 or (is_last_point and did_slow_log):
                    self.logger.info('{}/{} ({:.1f}%) points analysed at {:.2f} points/second.'.format(
                        i, len(new_points), i / (len(new_points) - 1) * 100, (i - log_i) / log_time_delta))
                    await asyncio.sleep(0)
                    log_time = now
                    log_i = i
                    did_slow_log = True
                    if new_new_points:
                        await self.new_points(new_new_points)
                        new_new_points = []
                    if new_off_route_points:
                        await self.off_route_tracker.new_points(new_off_route_points)
                        new_off_route_points = []

            if self.finished:
                break

        if new_new_points:
            await self.new_points(new_new_points)
        if new_off_route_points:
            await self.off_route_tracker.new_points(new_off_route_points)

    def get_predicted_position(self, time):
        # TODO if time > a position received - then interpolate between those positions.
        pp = self.prev_point_with_position
        if pp and not self.finished and time - pp['time'] < self.track_break_time and pp.get('speed_from_last', 0) > 3:
            closest = self.prev_closest
            time_from_last = (time - pp['time']).total_seconds()
            dist_moved_from_last = pp['speed_from_last'] / 3.6 * time_from_last
            if closest and self.going_forward and closest.dist < 500:
                # Predicted to follow route if they are on the route and going forward.
                dist_route = pp['dist_route'] + dist_moved_from_last
                proceeding_route = list(chain(
                    (closest.point, ),
                    closest.route['points'][closest.point_pair[1].index:],
                ))
                # TODO continue main route
                point_point = move_along_route(proceeding_route, dist_moved_from_last)
                point = {
                    'position': [point_point.lat, point_point.lng],
                    'dist_route': round(dist_route),
                }
                if 'elevation' in closest.route:
                    point['route_elevation'] = round(route_elevation(closest.route, dist_route))

                return point
            elif self.prev_unit_vector is not None:
                # Just keep going in the direction that they were going on.
                pv = (self.prev_unit_vector * dist_moved_from_last) + self.prev_point_with_position_point.pv
                nv = p_EB_E2n_EB_E(pv)
                new_point = Point.from_nv(nv[0])
                new_position = (new_point.lat, new_point.lng, pp['position'][2]) if len(pp['position']) == 3 else (new_point.lat, new_point.lng)
                return {
                    'position': new_position,
                }


@attr.s(slots=True)
class Point(object):
    lat = attr.ib()
    lng = attr.ib()
    _nv = attr.ib(default=None, repr=False, cmp=False)
    _pv = attr.ib(default=None, repr=False, cmp=False)

    def to_point(self):
        return self

    @property
    def nv(self):
        if self._nv is None:
            self._nv = lat_lon2n_E(deg2rad(self.lat), deg2rad(self.lng))
        return self._nv

    @property
    def pv(self):
        if self._pv is None:
            self._pv = n_EB_E2p_EB_E(self.nv)
        return self._pv

    @classmethod
    def from_nv(cls, nv, round_digits=6):
        lat, lng = n_E2lat_lon(nv)
        point = cls(round(rad2deg(lat[0]), round_digits), round(rad2deg(lng[0]), round_digits))
        point._nv = nv
        return point


@attr.s(slots=True)
class IndexedPoint(Point):
    index = attr.ib(default=None)
    distance = attr.ib(default=None)

    def to_point(self):
        return Point(self.lat, self.lng)


def get_analyse_routes(org_routes):
    return [get_analyse_route(route) for route in org_routes]


def get_analyse_route(org_route):
    route = copy.copy(org_route)
    route['points'] = route_points = route_with_distance_and_index(org_route['points'])
    route['point_pairs'] = [get_point_pair_precalc(*point_pair) for point_pair in pairs(route_points)]

    if route.get('simplified_points_indexes'):
        simplified_points = [route_points[i] for i in route['simplified_points_indexes']]
    else:
        logging.info('No pre-calculated simplified_points. Please run process_event_routes on event for faster start up.')
        if not route.get('split_at_dist'):
            simplified_points = ramer_douglas_peucker(route_points, 500)
        else:
            simplified_points = ramer_douglas_peucker_sections(route_points, 500, route['split_at_dist'], route['split_point_range'])

    route['simplfied_point_pairs'] = [get_point_pair_precalc(*point_pair) for point_pair in pairs(simplified_points)]
    logger.debug('Route points: {}, simplified points: {}, distance: {}'.format(len(route_points), len(route['simplfied_point_pairs']), route_points[-1].distance))
    return route


def route_with_distance_and_index(route):
    dist = 0
    previous_point = None

    def get_point(i, point):
        nonlocal dist
        nonlocal previous_point
        point = IndexedPoint(*point, index=i)
        if previous_point:
            dist += distance(previous_point, point)
        point.distance = dist
        previous_point = point
        return point
    return [get_point(i, point) for i, point in enumerate(route)]


def distance(point1, point2):
    dist = norm(point1.pv - point2.pv)
    return dist


def pairs(items):
    itr = iter(items)
    item1 = next(itr)
    for item2 in itr:
        yield item1, item2
        item1 = item2


dist_attr_getter = operator.attrgetter('dist')


def ramer_douglas_peucker(points, epsilon):
    if len(points) > 2:
        c_points = (find_c_point(point, points[0], points[-1]) for point in points[1:-1])
        imax, (dmax, _) = max(enumerate(c_points), key=lambda item: item[1].dist)
    else:
        dmax = 0

    if dmax > epsilon:
        r1 = ramer_douglas_peucker(points[:imax + 2], epsilon)
        r2 = ramer_douglas_peucker(points[imax + 1:], epsilon)
        return r1[:-1] + r2
    else:
        return (points[0], points[-1])


def ramer_douglas_peucker_sections(points, epsilon, split_at_dist, split_point_range):
    simplified_points_sections = []
    last_index = 0
    for dist in split_at_dist:
        min_dist = dist - split_point_range
        max_dist = dist + split_point_range
        close_points = [point for point in points if min_dist <= point.distance < max_dist]
        simplified_close_points = ramer_douglas_peucker(close_points, epsilon)
        closest_point = min(simplified_close_points, key=lambda point: abs(dist - point.distance))
        closest_index = closest_point.index
        simplified_points_section = ramer_douglas_peucker(points[last_index:closest_index + 1], epsilon)
        simplified_points_sections.append(simplified_points_section[:-1])
        last_index = closest_index

    simplified_points_sections.append(ramer_douglas_peucker(points[last_index:], epsilon))
    return list(chain.from_iterable(simplified_points_sections))


find_closest_point_pair_routes_result = collections.namedtuple('closest_point_pair_route', ('route_i', 'route', 'point_pair', 'dist', 'point'))


def find_closest_point_pair_routes_cache_key(routes, to_point, min_search_complex_dist, prev_closest_route_i, break_out_dist, prev_dist):
    return to_point.lat, to_point.lng, min_search_complex_dist, prev_closest_route_i, break_out_dist, prev_dist


def find_closest_point_pair_routes_pack(result):
    return result.route_i, result.point_pair[0].index, result.dist, result.point.lat, result.point.lng


def find_closest_point_pair_routes_unpack(routes, packed):
    route_i, point_pair_index, dist, lat, lng = packed
    route = routes[route_i]
    point_pair = route['point_pairs'][point_pair_index]
    point = Point(lat, lng)
    return find_closest_point_pair_routes_result(route_i, route, point_pair, dist, point)


def find_closest_point_pair_routes(routes, to_point, min_search_complex_dist, prev_closest_route_i, break_out_dist, prev_dist):
    results = []
    if routes:
        special_routes = (0, )
        if prev_closest_route_i:
            special_routes += (prev_closest_route_i, )

        for route_i in reversed(special_routes):
            route = routes[route_i]
            result = find_closest_point_pair_routes_result(
                route_i, route, *find_closest_point_pair_route(route, to_point, min_search_complex_dist, prev_dist))
            if result.dist < break_out_dist:
                return result
            results.append(result)

        for route_i, route in enumerate(routes):
            if route_i not in special_routes:
                results.append(find_closest_point_pair_routes_result(
                    route_i, route, *find_closest_point_pair_route(route, to_point, min_search_complex_dist, prev_dist)))

        return min(results, key=dist_attr_getter)


find_closest_point_pair_result = collections.namedtuple('closest_point_pair', ('point_pair', 'dist', 'point'))


def find_closest_point_pair_route(route, to_point, min_search_complex_dist, prev_dist=None):
    simplified_closest = find_closest_point_pair(route, route['simplfied_point_pairs'], to_point, prev_dist)
    if simplified_closest.dist > min_search_complex_dist or simplified_closest.point_pair[0].index == simplified_closest.point_pair[1].index - 1:
        return simplified_closest
    else:
        return find_closest_point_pair(route, route['point_pairs'][simplified_closest.point_pair[0].index: simplified_closest.point_pair[1].index + 1], to_point, prev_dist)


def find_closest_point_pair(route, point_pairs, to_point, prev_dist):
    with_c_points = [find_closest_point_pair_result(point_pair[:2], *find_c_point_from_precalc(to_point, *point_pair))
                     for point_pair in point_pairs]

    circular_range = route.get('circular_range')
    if prev_dist is not None and circular_range:
        def min_key(closest):
            move_distance = abs(route_distance(route, closest) - prev_dist)
            move_distnace_adj = pow(2, (move_distance - circular_range) / 1000)
            rank = closest.dist + move_distnace_adj
            # print(move_distance, n, move_distnace_adj, closest.dist, rank)
            return rank
    else:
        min_key = dist_attr_getter

    r = min(with_c_points, key=min_key)
    # print(f'return {r}')
    return r


def route_distance(route, closest):
    prev_route_point = closest.point_pair[0]
    if route['main']:
        return round(prev_route_point.distance + distance(prev_route_point, closest.point))
    else:
        alt_route_dist = prev_route_point.distance + distance(prev_route_point, closest.point)
        return round(alt_route_dist * route['dist_factor'] + route['start_distance'])


def route_distance_no_adjust(route, closest):
    prev_route_point = closest.point_pair[0]
    return round(prev_route_point.distance + distance(prev_route_point, closest.point))


def route_elevation(route, route_dist):
    elevation = route['elevation']
    if route['main']:
        dist_on_route = route_dist
    else:
        dist_on_route = (route_dist - route['start_distance']) / route['dist_factor']
    point2_i = bisect.bisect(KeyifyList(elevation, itemgetter(3)), dist_on_route)
    if point2_i == len(elevation):
        point2_i -= 1

    point2 = elevation[point2_i]
    point1 = elevation[point2_i - 1]

    dist_factor = (dist_on_route - point2[3]) / (point2[3] - point1[3])
    return ((point2[2] - point1[2]) * dist_factor) + point2[2]


find_c_point_result = collections.namedtuple('c_point', ('dist', 'point'))


def find_c_point(to_point, point1, point2):
    return find_c_point_from_precalc(to_point, *get_point_pair_precalc(point1, point2))


def arccos_limit(n):
    if n > 1:
        return 1
    if n < -1:
        return -1
    return n


def find_c_point_from_precalc(to_point, point1, point2, c12, p1h, p2h, dp1p2):
    tpn = to_point.nv
    ctp = cross(tpn, c12, axis=0)
    try:
        c = unit(cross(ctp, c12, axis=0))
    except Exception:
        print((to_point, point1, point2))
        raise
    sutable_c = None
    for co in (c, 0 - c):
        co_rs = co.reshape((3, ))
        dp1co = arccos(arccos_limit(dot(p1h, co_rs)))
        dp2co = arccos(arccos_limit(dot(p2h, co_rs)))
        if abs(dp1co + dp2co - dp1p2) < 0.000001:
            sutable_c = co
            break

    if sutable_c is not None:
        c_point_lat, c_point_lng = n_E2lat_lon(sutable_c)
        c_point = Point(lat=rad2deg(c_point_lat[0]), lng=rad2deg(c_point_lng[0]))
        c_dist = distance(to_point, c_point)
    else:
        c_dist, c_point = min(((distance(to_point, p), p) for p in (point1, point2)))

    return find_c_point_result(c_dist, c_point)


def get_point_pair_precalc(point1, point2):
    p1 = point1.nv
    p2 = point2.nv
    c12 = cross(p1, p2, axis=0)
    p1h = p1.reshape((3, ))
    p2h = p2.reshape((3, ))
    dp1p2 = arccos(dot(p1h, p2h))
    return point1, point2, c12, p1h, p2h, dp1p2


def get_equal_spaced_points(points, dist_between_points, start_dist=0, round_digits=6):
    cum_dist = start_dist
    yield (points[0], cum_dist)
    dist_from_last_step = 0
    last_point = points[0]
    for point in points[1:]:
        point_distance = distance(last_point, point)
        point_dist_remaining = point_distance + dist_from_last_step
        while point_dist_remaining > dist_between_points:
            point_dist_remaining -= dist_between_points
            cum_dist += dist_between_points
            new_point_nv = interpolate((last_point.nv, point.nv), (point_distance - point_dist_remaining) / point_distance)
            new_point = Point.from_nv(new_point_nv, round_digits=round_digits)
            yield (new_point, cum_dist)
        dist_from_last_step = point_dist_remaining
        last_point = point
    cum_dist += dist_from_last_step
    yield (points[-1], cum_dist)


def move_along_route(route, dist):
    for i, (point1, point2) in enumerate(pairs(route)):
        dist_between = point2.distance - point1.distance if isinstance(point1, IndexedPoint) and isinstance(point2, IndexedPoint) else distance(point1, point2)
        if dist > dist_between:
            dist -= dist_between
        else:
            ti = dist / dist_between
            nv = interpolate((point1.nv, point2.nv), ti)
            return Point.from_nv(nv)
    else:
        return point2


class KeyifyList(object):
    def __init__(self, inner, key):
        self.inner = inner
        self.key = key

    def __len__(self):
        return len(self.inner)

    def __getitem__(self, k):
        return self.key(self.inner[k])
