import collections
import heapq
import json
import math
import os
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from threading import Event, Lock, RLock, Thread
from typing import Dict, List, Optional, Tuple

import numpy as np

from db.dbWrapperBase import DbWrapperBase
from geofence.geofenceHelper import GeofenceHelper
from route.routecalc.ClusteringHelper import ClusteringHelper
from route.routecalc.calculate_route import getJsonRoute
from utils.collections import Location
from utils.logging import logger
from utils.walkerArgs import parseArgs

args = parseArgs()

Relation = collections.namedtuple(
        'Relation', ['other_event', 'distance', 'timedelta'])


@dataclass
class RoutePoolEntry:
    last_access: float
    queue: collections.deque
    subroute: List[Location]


class RouteManagerBase(ABC):
    def __init__(self, db_wrapper: DbWrapperBase, coords: List[Location], max_radius: float,
                 max_coords_within_radius: int, path_to_include_geofence: str, path_to_exclude_geofence: str,
                 routefile: str, mode=None, init: bool = False, name: str = "unknown", settings: dict = None,
                 level: bool = False, calctype: str = "optimized"):
        self.db_wrapper: DbWrapperBase = db_wrapper
        self.init: bool = init
        self.name: str = name
        self._coords_unstructured: List[Location] = coords
        self.geofence_helper: GeofenceHelper = GeofenceHelper(
                path_to_include_geofence, path_to_exclude_geofence)
        self._routefile = os.path.join(args.file_path, routefile)
        self._max_radius: float = max_radius
        self._max_coords_within_radius: int = max_coords_within_radius
        self.settings: dict = settings
        self.mode = mode
        self._is_started: bool = False
        self._first_started = False
        self._current_route_round_coords: List[Location] = []
        self._start_calc: bool = False
        self._rounds = {}
        self._positiontyp = {}
        self._coords_to_be_ignored = set()
        self._level = level
        self._calctype = calctype
        self._overwrite_calculation: bool = False
        self._stops_not_processed: Dict[Location, int] = {}
        self._routepool: Dict[str, RoutePoolEntry] = {}
        # self._routepoolpositionmax: Dict = {}

        # we want to store the workers using the routemanager
        self._workers_registered: List[str] = []
        self._workers_registered_mutex = Lock()

        # waiting till routepool is filled up
        self._workers_fillup_mutex = Lock()

        self._last_round_prio = {}
        self._manager_mutex = RLock()
        self._round_started_time = None
        self._route: List[Location] = []

        if coords is not None:
            if init:
                fenced_coords = coords
            else:
                fenced_coords = self.geofence_helper.get_geofenced_coordinates(
                        coords)
            new_coords = getJsonRoute(
                    fenced_coords, max_radius, max_coords_within_radius, routefile,
                    algorithm=calctype)
            for coord in new_coords:
                self._route.append(Location(coord["lat"], coord["lng"]))
        self._current_index_of_route = 0
        self._init_mode_rounds = 0

        if self.settings is not None:
            self.delay_after_timestamp_prio = self.settings.get(
                    "delay_after_prio_event", None)
            self.starve_route = self.settings.get("starve_route", False)
        else:
            self.delay_after_timestamp_prio = None
            self.starve_route = False

        # initialize priority queue variables
        self._prio_queue = None
        self._update_prio_queue_thread = None
        self._stop_update_thread = Event()
        self._init_route_queue()

    def get_ids_iv(self) -> Optional[List[int]]:
        if self.settings is not None:
            return self.settings.get("mon_ids_iv_raw", [])
        else:
            return None

    def stop_routemanager(self):
        if self._update_prio_queue_thread is not None:
            self._stop_update_thread.set()
            self._update_prio_queue_thread.join()

    def _init_route_queue(self):
        self._manager_mutex.acquire()
        try:
            if len(self._route) > 0:
                self._current_route_round_coords.clear()
                logger.debug("Creating queue for coords")
                for latlng in self._route:
                    self._current_route_round_coords.append(latlng)
                logger.debug("Finished creating queue")
        finally:
            self._manager_mutex.release()

    def _clear_coords(self):
        self._manager_mutex.acquire()
        self._coords_unstructured = None
        self._manager_mutex.release()

    def register_worker(self, worker_name) -> bool:
        self._workers_registered_mutex.acquire()
        try:
            if worker_name in self._workers_registered:
                logger.info("Worker {} already registered to routemanager {}", str(
                        worker_name), str(self.name))
                return False
            else:
                logger.info("Worker {} registering to routemanager {}",
                            str(worker_name), str(self.name))
                self._workers_registered.append(worker_name)
                self._rounds[worker_name] = 0
                self._positiontyp[worker_name] = 0

                # if worker_name not in self._routepool:
                #     self._routepool[worker_name] = RoutePoolEntry(time.time(), collections.deque(), [])
                # self.__worker_changed_update_routepools()
                return True

        finally:
            self._workers_registered_mutex.release()

    def unregister_worker(self, worker_name):
        self._workers_registered_mutex.acquire()
        try:
            if worker_name in self._workers_registered:
                logger.info("Worker {} unregistering from routemanager {}", str(
                        worker_name), str(self.name))
                self._workers_registered.remove(worker_name)
                if worker_name in self._routepool:
                    logger.info('Cleanup routepool for origin {}', str(worker_name))
                    del self._routepool[worker_name]
                del self._rounds[worker_name]
            else:
                # TODO: handle differently?
                logger.info(
                        "Worker {} failed unregistering from routemanager {} since subscription was previously lifted",
                        str(
                                worker_name), str(self.name))
            if len(self._workers_registered) == 0 and self._is_started:
                logger.info(
                        "Routemanager {} does not have any subscribing workers anymore, calling stop", str(self.name))
                self._quit_route()
        finally:
            self._workers_registered_mutex.release()

    def stop_worker(self):
        self._workers_registered_mutex.acquire()
        try:
            for worker in self._workers_registered:
                logger.info("Worker {} stopped from routemanager {}", str(
                        worker), str(self.name))
                worker.stop_worker()
                self._workers_registered.remove(worker)
                if worker in self._routepool:
                    logger.info('Cleanup routepool for origin {}', str(worker))
                    del self._routepool[worker]
                del self._rounds[worker]
            if len(self._workers_registered) == 0 and self._is_started:
                logger.info(
                        "Routemanager {} does not have any subscribing workers anymore, calling stop", str(self.name))
                self._quit_route()
        finally:
            self._workers_registered_mutex.release()

    def _check_started(self):
        return self._is_started

    def _start_priority_queue(self):
        if (self._update_prio_queue_thread is None and (self.delay_after_timestamp_prio is not None or self.mode ==
                                                        "iv_mitm") and not self.mode == "pokestops"):
            self._prio_queue = []
            if self.mode not in ["iv_mitm", "pokestops"]:
                self.clustering_helper = ClusteringHelper(self._max_radius,
                                                          self._max_coords_within_radius,
                                                          self._cluster_priority_queue_criteria())
            self._update_prio_queue_thread = Thread(name="prio_queue_update_" + self.name,
                                                    target=self._update_priority_queue_loop)
            self._update_prio_queue_thread.daemon = False
            self._update_prio_queue_thread.start()

    # list_coords is a numpy array of arrays!
    def add_coords_numpy(self, list_coords: np.ndarray):
        fenced_coords = self.geofence_helper.get_geofenced_coordinates(
                list_coords)
        self._manager_mutex.acquire()
        if self._coords_unstructured is None:
            self._coords_unstructured = fenced_coords
        else:
            self._coords_unstructured = np.concatenate(
                    (self._coords_unstructured, fenced_coords))
        self._manager_mutex.release()

    def add_coords_list(self, list_coords: List[Location]):
        to_be_appended = np.zeros(shape=(len(list_coords), 2))
        for i in range(len(list_coords)):
            to_be_appended[i][0] = float(list_coords[i].lat)
            to_be_appended[i][1] = float(list_coords[i].lng)
        self.add_coords_numpy(to_be_appended)

    def calculate_new_route(self, coords, max_radius, max_coords_within_radius, routefile, delete_old_route,
                            num_procs=0):
        if self._overwrite_calculation:
            calctype = 'quick'
        else:
            calctype = self._calctype

        if delete_old_route and os.path.exists(str(routefile) + ".calc"):
            logger.debug("Deleting routefile...")
            os.remove(str(routefile) + ".calc")
        new_route = getJsonRoute(coords, max_radius, max_coords_within_radius, num_processes=num_procs,
                                 routefile=routefile, algorithm=calctype)
        if self._overwrite_calculation:
            self._overwrite_calculation = False
        return new_route

    def empty_routequeue(self):
        return len(self._current_route_round_coords) > 0

    def recalc_route(self, max_radius: float, max_coords_within_radius: int, num_procs: int = 1,
                     delete_old_route: bool = False, nofile: bool = False):
        current_coords = self._coords_unstructured
        if nofile:
            routefile = None
        else:
            routefile = self._routefile
        new_route = self.calculate_new_route(current_coords, max_radius, max_coords_within_radius,
                                             routefile, delete_old_route, num_procs)
        self._manager_mutex.acquire()
        self._route.clear()
        for coord in new_route:
            self._route.append(Location(coord["lat"], coord["lng"]))
        self._current_route_round_coords = self._route.copy()
        self._current_index_of_route = 0
        self._manager_mutex.release()

    def _update_priority_queue_loop(self):
        if self._priority_queue_update_interval() is None or self._priority_queue_update_interval() == 0:
            return
        while not self._stop_update_thread.is_set():
            # retrieve the latest hatches from DB
            # newQueue = self._db_wrapper.get_next_raid_hatches(self._delayAfterHatch, self._geofenceHelper)
            new_queue = self._retrieve_latest_priority_queue()
            self._merge_priority_queue(new_queue)
            time.sleep(self._priority_queue_update_interval())

            # for now, let's call the regular checkup on routepools here...
            self._check_routepools()

    def _merge_priority_queue(self, new_queue):
        if new_queue is not None:
            self._manager_mutex.acquire()
            merged = list(new_queue)
            logger.info("New raw priority queue with {} entries", len(merged))
            merged = self._filter_priority_queue_internal(merged)
            heapq.heapify(merged)
            self._prio_queue = merged
            self._manager_mutex.release()
            logger.info("New clustered priority queue with {} entries", len(merged))
            logger.debug("Priority queue entries: {}", str(merged))

    def date_diff_in_seconds(self, dt2, dt1):
        timedelta = dt2 - dt1
        return timedelta.days * 24 * 3600 + timedelta.seconds

    def dhms_from_seconds(self, seconds):
        minutes, seconds = divmod(seconds, 60)
        hours, minutes = divmod(minutes, 60)
        # days, hours = divmod(hours, 24)
        return hours, minutes, seconds

    def _get_round_finished_string(self):
        round_finish_time = datetime.now()
        round_completed_in = (
                "%d hours, %d minutes, %d seconds" % (
            self.dhms_from_seconds(
                    self.date_diff_in_seconds(
                            round_finish_time, self._round_started_time)
            )
        )
        )
        return round_completed_in

    def add_coord_to_be_removed(self, lat: float, lon: float):
        if lat < -90.0 or lat > 90.0 or lon < -180.0 or lon > 180.0:
            return
        self._manager_mutex.acquire()
        self._coords_to_be_ignored.add(Location(lat, lon))
        self._manager_mutex.release()

    @abstractmethod
    def _retrieve_latest_priority_queue(self):
        """
        Method that's supposed to return a plain list containing (timestamp, Location) of the next events of interest
        :return:
        """
        pass

    @abstractmethod
    def _start_routemanager(self):
        """
        Starts priority queue or whatever the implementations require
        :return:
        """
        pass

    @abstractmethod
    def _quit_route(self):
        """
        Killing the Route Thread
        :return:
        """
        pass

    @abstractmethod
    def _get_coords_post_init(self):
        """
        Return list of coords to be fetched and used for routecalc
        :return:
        """
        pass

    @abstractmethod
    def _check_coords_before_returning(self, lat, lng):
        """
        Return list of coords to be fetched and used for routecalc
        :return:
        """
        pass

    @abstractmethod
    def _recalc_route_workertype(self):
        """
        Return a new route for worker
        :return:
        """
        pass

    @abstractmethod
    def _get_coords_after_finish_route(self):
        """
        Return list of coords to be fetched after finish a route
        :return:
        """
        pass

    @abstractmethod
    def _cluster_priority_queue_criteria(self):
        """
        If you do not want to have any filtering, simply return 0, 0, otherwise simply
        return timedelta_seconds, distance
        :return:
        """

    @abstractmethod
    def _priority_queue_update_interval(self):
        """
        The time to sleep in between consecutive updates of the priority queue
        :return:
        """

    @abstractmethod
    def _delete_coord_after_fetch(self) -> bool:
        """
        Whether coords fetched from get_next_location should be removed from the total route
        :return:
        """

    def _filter_priority_queue_internal(self, latest):
        """
        Filter through the internal priority queue and cluster events within the timedelta and distance returned by
        _cluster_priority_queue_criteria
        :return:
        """
        # timedelta_seconds = self._cluster_priority_queue_criteria()
        if self.mode == "iv_mitm":
            # exclude IV prioQ to also pass encounterIDs since we do not pass additional information through when
            # clustering
            return latest
        delete_seconds_passed = 0
        if self.settings is not None:
            delete_seconds_passed = self.settings.get(
                    "remove_from_queue_backlog", 0)

        if delete_seconds_passed is not None:
            delete_before = time.time() - delete_seconds_passed
        else:
            delete_before = 0
        latest = [to_keep for to_keep in latest if not to_keep[0] < delete_before]
        # TODO: sort latest by modified flag of event
        # merged = self._merge_queue(latest, self._max_radius, 2, timedelta_seconds)
        merged = self.clustering_helper.get_clustered(latest)
        return merged

    def get_next_location(self, origin: str) -> Optional[Location]:
        if len(self._route) == 0:
            self._recalc_route_workertype()
        if origin not in self._routepool:
            self._routepool[origin] = RoutePoolEntry(time.time(), collections.deque(), [])
            self.__worker_changed_update_routepools()
            # self._routepoolpositionmax[origin] = 0
        logger.debug("get_next_location of {} called", str(self.name))
        if not self._is_started:
            logger.info(
                    "Starting routemanager {} in get_next_location", str(self.name))
            self._start_routemanager()
        next_lat, next_lng = 0, 0

        if self._start_calc:
            logger.info("Another process already calculate the new route")
            return None

        # first check if a location is available, if not, block until we have one...
        got_location = False
        while not got_location and self._is_started and not self.init:
            logger.debug(
                    "{}: Checking if a location is available...", str(self.name))
            self._manager_mutex.acquire()
            got_location = len(self._current_route_round_coords) > 0 or len(self._routepool[origin].queue) > 0 or (
                    self._prio_queue is not None and len(self._prio_queue) > 0)
            self._manager_mutex.release()
            if not got_location:
                logger.debug("{}: No location available yet", str(self.name))
                if self._get_coords_after_finish_route() and not self.init:
                    # getting new coords or IV worker
                    time.sleep(1)
                else:
                    logger.info("Not getting new coords - leaving worker")
                    return None

        logger.debug(
                "{}: Location available, acquiring lock and trying to return location", str(self.name))
        self._manager_mutex.acquire()
        # check priority queue for items of priority that are past our time...
        # if that is not the case, simply increase the index in route and return the location on route

        # determine whether we move to the next location or the prio queue top's item
        if (self.delay_after_timestamp_prio is not None and ((not self._last_round_prio.get(origin, False)
                                                              or self.starve_route)
                                                             and self._prio_queue and len(self._prio_queue) > 0
                                                             and self._prio_queue[0][0] < time.time())):
            logger.debug("{}: Priority event", str(self.name))
            next_coord = heapq.heappop(self._prio_queue)[1]
            self._last_round_prio[origin] = True
            self._positiontyp[origin] = 1
            logger.info("Round of route {} is moving to {}, {} for a priority event", str(
                    self.name), str(next_coord.lat), str(next_coord.lng))
        else:
            logger.debug("{}: Moving on with route", str(self.name))
            self._positiontyp[origin] = 0
            # TODO: this check is likely always true now.............
            if len(self._route) == len(self._current_route_round_coords):
                if self._round_started_time is not None:
                    logger.info("Round of route {} reached the first spot again. It took {}", str(
                            self.name), str(self._get_round_finished_string()))
                    self.add_route_to_origin()
                self._round_started_time = datetime.now()
                if len(self._route) == 0:
                    return None
                logger.info("Round of route {} started at {}", str(
                        self.name), str(self._round_started_time))
            elif self._round_started_time is None:
                self._round_started_time = datetime.now()

            # continue as usual
            if self.init and len(self._current_route_round_coords) == 0:
                self._init_mode_rounds += 1
            if self.init and len(self._current_route_round_coords) == 0 and \
                    self._init_mode_rounds >= int(self.settings.get("init_mode_rounds", 1)) and \
                    len(self._routepool[origin].queue) == 0:
                # we are done with init, let's calculate a new route
                logger.warning("Init of {} done, it took {}, calculating new route...", str(
                        self.name), self._get_round_finished_string())
                if self._start_calc:
                    logger.info(
                            "Another process already calculate the new route")
                    self._manager_mutex.release()
                    return None
                self._start_calc = True
                self._clear_coords()
                coords = self._get_coords_post_init()
                logger.debug("Setting {} coords to as new points in route of {}", str(
                        len(coords)), str(self.name))
                self.add_coords_list(coords)
                logger.debug("Route of {} is being calculated", str(self.name))
                self._recalc_route_workertype()
                self.init = False
                self.change_init_mapping(self.name)
                self._manager_mutex.release()
                self._start_calc = False
                logger.debug(
                        "Initroute of {} is finished - restart worker", str(self.name))
                return None
            elif len(self._current_route_round_coords) > 1 and len(self._routepool[origin].queue) == 0:
                self.__worker_changed_update_routepools()
            elif len(self._current_route_round_coords) == 1 and len(self._routepool[origin].queue) == 0:
                logger.info('Reaching last coord of route')
            elif len(self._current_route_round_coords) == 0 and len(self._routepool[origin].queue) == 0:
                # normal queue is empty - prioQ is filled. Try to generate a new Q
                logger.info("Normal routequeue is empty - try to fill up")
                if self._get_coords_after_finish_route():
                    # getting new coords or IV worker
                    self._manager_mutex.release()
                    return self.get_next_location(origin)
                elif not self._get_coords_after_finish_route():
                    logger.info("Not getting new coords - leaving worker")
                    self._manager_mutex.release()
                    return None
                self._manager_mutex.release()

            # getting new coord
            if len(self._routepool[origin].queue) == 0:
                if not self.__worker_changed_update_routepools():
                    return None

            next_coord = self._routepool[origin].queue.popleft()
            self._routepool[origin].last_access = time.time()
            if self._delete_coord_after_fetch() and next_coord in self._current_route_round_coords:
                self._current_route_round_coords.remove(next_coord)
            logger.info("{}: Moving on with location {} [{} coords left (Workerpool) - {} coords left (Route)]",
                        str(self.name), str(next_coord), str(len(self._routepool[origin].queue))
                        , str(len(self._current_route_round_coords)))

            self._last_round_prio[origin] = False
        logger.debug("{}: Done grabbing next coord, releasing lock and returning location: {}", str(
                self.name), str(next_coord))
        self._manager_mutex.release()
        if self._check_coords_before_returning(next_coord.lat, next_coord.lng):
            if self._delete_coord_after_fetch() and next_coord in self._current_route_round_coords:
                self._current_route_round_coords.remove(next_coord)
            return next_coord
        else:
            return self.get_next_location(origin)

    def _fill_queue_of_worker(self, origin: str):
        if len(self._current_route_round_coords) == 0:
            logger.warning('Routepool for {} is empty now - worker {} get no coords - leaving'.format(str(self.name),
                                                                                                      str(origin)))
            return False
        self.__worker_changed_update_routepools()
        return True

    # to be called regularly to remove inactive workers that used to be registered
    def _check_routepools(self, timeout: int = 300):
        routepool_changed: bool = False
        with self._manager_mutex:
            for origin in list(self._routepool):
                entry: RoutePoolEntry = self._routepool[origin]
                if time.time() - entry.last_access > timeout:
                    logger.warning(
                            "Worker {} has not accessed a location in {} seconds, removing from routemanager".format(
                                    origin, timeout))
                    del self._routepool[origin]
                    routepool_changed = True
        if routepool_changed:
            self.__worker_changed_update_routepools()

    def __worker_changed_update_routepools(self):
        if len(self._route) == 0:
            self._recalc_route_workertype()
        with self._manager_mutex:
            logger.info("Updating all routepools because of removal/addition")
            if len(self._workers_registered) == 0:
                logger.info("No registered workers, aborting __worker_changed_update_routepools...")
                return
            new_subroute_length = math.ceil(len(self._current_route_round_coords) / len(self._workers_registered))
            i: int = 0
            for origin in self._routepool.keys():
                # let's assume a worker has already been removed or added to the dict (keys)...
                entry: RoutePoolEntry = self._routepool[origin]

                if len(self._current_route_round_coords) % 2 == 0:
                    new_subroute: List[Location] = [self._current_route_round_coords[index] for index in
                                                    range(i * new_subroute_length,
                                                          ((i + 1) * new_subroute_length))]
                else:
                    new_subroute: List[Location] = [self._current_route_round_coords[index] for index in
                                                    range(i * new_subroute_length,
                                                          ((i + 1) *
                                                           new_subroute_length) -1)]
                i += 1
                if len(entry.subroute) == 0:
                    # worker is freshly registering, pass him his fair share
                    entry.subroute = new_subroute
                    for loc in new_subroute:
                        entry.queue.append(loc)
                if len(new_subroute) == len(entry.subroute):
                    # apparently nothing changed
                    # TODO: check for equivalance and if queues are filled...
                    logger.info("Apparently no changes in subroutes...")
                elif len(new_subroute) < len(entry.subroute):
                    # we apparently have added at least a worker...
                    #   1) reduce the start of the current queue to start of new route
                    #   2) append the coords missing (check end of old routelength, add/remove from there on compared
                    #      to new)
                    old_queue: collections.deque = collections.deque(entry.queue)
                    while len(old_queue) > 0 and old_queue.popleft() != new_subroute[0]:
                        pass

                    if len(old_queue) == 0:
                        # just set new route...
                        entry.queue: collections.deque = collections.deque()
                        for location in new_subroute:
                            entry.queue.append(location)
                        continue

                    # TODO: what if old_queue is beyond new?
                    # we now are at a point where we need to also check the end of the old queue and
                    # append possibly missing coords to it
                    last_el_old_q: Location = old_queue[len(old_queue) - 1]
                    if last_el_old_q in new_subroute:
                        # we have the last element in the old subroute, we can actually append stuff with the diff to
                        # the new route
                        new_subroute_copy = collections.deque(new_subroute)
                        while len(new_subroute_copy) > 0 and new_subroute_copy.popleft() != last_el_old_q:
                            pass
                        logger.info("Length of subroute to be extended by {}".format(str(len(new_subroute_copy))))
                        while len(new_subroute_copy) > 0:
                            entry.queue.append(new_subroute_copy.popleft())

                elif len(new_subroute) > len(entry.subroute) > 0:
                    #   old routelength < new len(route)/n:
                    #   we have likely removed a worker and need to redistribute
                    #   1) fetch start and end of old queue
                    #   2) we sorta ignore start/what's been visited so far
                    #   3) if the end is not part of the new route, check for the last coord of the current route
                    #   still in
                    #   the new route, remove the old rest of it (or just fetch the first coord of the next subroute and
                    #   remove the coords of that coord onward)
                    last_el_old_route: Location = entry.subroute[len(entry.subroute) - 1]
                    old_queue_list: List[Location] = list(entry.queue)

                    last_el_new_route: Location = new_subroute[len(new_subroute) - 1]
                    # check last element of new subroute:
                    if last_el_new_route is not None and last_el_new_route in old_queue_list:
                        # if in current queue, remove from end of new subroute to end of old queue
                        del old_queue_list[old_queue.index(last_el_new_route): len(old_queue_list) - 1]
                    elif last_el_old_route in new_subroute:
                        # append from end of queue (compared to new subroute) to end of new subroute
                        missing_new_route_part: List[Location] = new_subroute.copy()
                        del missing_new_route_part[0: new_subroute.index(last_el_old_route)]
                        old_queue_list.extend(missing_new_route_part)

                    entry.queue = collections.deque()
                    [entry.queue.append(i) for i in old_queue_list]

                if len(entry.queue) == 0:
                    [entry.queue.append(i) for i in new_subroute]
                # don't forget to update the subroute ;)
                entry.subroute = new_subroute
            # TODO: A worker has been removed or added, we need to update the individual workerpools/queues
            #
            # First: Split the original route by the remaining workers => we have a list of new subroutes of
            # len(route)/n coordinates
            #
            # Iterate over all remaining routepools
            # Possible situations now:
            #
            #   Routelengths == new len(route)/n:
            #   Apparently nothing has changed...
            #
            #   old routelength > new len(route)/n:
            #   we have likely added a worker and need to redistribute
            #   1) reduce the start of the current queue to start after the end of the previous pool
            #   2) append the coords missing (check end of old routelength, add/remove from there on compared to new)

            #
            #   old routelength < new len(route)/n:
            #   we have likely removed a worker and need to redistribute
            #   1) fetch start and end of old queue
            #   2) we sorta ignore start/what's been visited so far
            #   3) if the end is not part of the new route, check for the last coord of the current route still in
            #   the new route, remove the old rest of it (or just fetch the first coord of the next subroute and
            #   remove the coords of that coord onward)

    def get_worker_workerpool(self):
        for origin in self._routepool:
            logger.info('Worker {}: {} open positions (Route: {})'.format(str(origin),
                                                                          str(len(self._routepool[origin].queue)),
                                                                          str(self.name)))

    def change_init_mapping(self, name_area: str):
        with open(args.mappings) as f:
            vars = json.load(f)

        for var in vars['areas']:
            if (var['name']) == name_area:
                var['init'] = bool(False)

        with open(args.mappings, 'w') as outfile:
            json.dump(vars, outfile, indent=4, sort_keys=True)

    def get_route_status(self, origin) -> Tuple[int, int]:
        if self._route:
            return len(self._route) - len(self._current_route_round_coords), len(self._route)
            # return (self._routepoolpositionmax[origin] - self._routepool[origin].qsize()), len(self._route)
        return 1, 1

    def get_rounds(self, origin: str) -> int:
        return self._rounds.get(origin, 999)

    def add_route_to_origin(self):
        for origin in self._rounds:
            self._rounds[origin] += 1

    def get_registered_workers(self) -> int:
        return len(self._workers_registered)

    def get_position_type(self, origin: str) -> Optional[str]:
        return self._positiontyp.get(origin, None)

    def get_geofence_helper(self) -> Optional[GeofenceHelper]:
        return self.geofence_helper

    def get_init(self) -> bool:
        return self.init

    def get_mode(self):
        return self.mode

    def get_settings(self) -> Optional[dict]:
        return self.settings

    def get_current_route(self) -> List[Location]:
        return self._route

    def get_current_prioroute(self) -> List[Location]:
        return self._prio_queue

    def get_level_mode(self):
        return self._level
