"""
This module contains a function that creates statistics which wraps a protocol instance and it's methods. Implements
a call chain for each of the protocol interface's methods.

Use this module through the **create_statistics**][gradys_embedded.protocol.plugin.statistics.create_statistics] method,
**never** instantiate the StatisticsProtocolWrapper directly.

Beware that this module uses monkey patching and may result in broken protocols if someone else tries to tamper with
the protocol's methods.
"""

import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import pandas as pd
from gradys_embedded.protocol.plugin.dispatcher import (
    create_dispatcher,
    dispose_dispatcher,
    DispatchReturn,
)

from gradys_embedded.protocol.interface import IProtocol


DATA_COLLECTION_INTERVAL = 0.5

# Rewrite the CSVs every N collected samples. At the default 0.5s interval that
# caps data loss from an unclean shutdown at ~15s, for a full rewrite of a few
# hundred rows -- cheap enough on a Pi, and these files stay small.
DATA_FLUSH_EVERY = 30

def handle_timer_srt(protocol: IProtocol, timer: str) -> DispatchReturn:
    """'
    Starts collection of statistics when a timer with a name 'statistics' is scheduled 

    Args:
        protocol: Protocol for which statistics should be collected
        timer: The name of the scheduled timer if existent
    """
    if timer == "statistics":
        wrapper = _statistics_protocol_wrappers.get(protocol)
        if wrapper is None:
            # Statistics were finished while this timer was already scheduled.
            return DispatchReturn.INTERRUPT

        wrapper.update_srt_statistic(
            protocol.provider.current_time(), time.time()
        )

        wrapper.update_tracked_variable_statistic(
            protocol.provider.current_time(), protocol.provider.tracked_variables
        )

        # The interval lives on the wrapper, not the protocol. Reading it from
        # the protocol raised AttributeError here, so the very first statistics
        # timer died before rescheduling and every run produced a
        # simulation_real_time CSV with a single row.
        protocol.provider.schedule_timer(
            "statistics",
            protocol.provider.current_time() + wrapper._statistics_collection_interval,
        )

        return DispatchReturn.INTERRUPT

    else:
        return DispatchReturn.CONTINUE


def handle_packet_tv(protocol: IProtocol, message: str) -> DispatchReturn:
    """'
    Starts collection of tracked variables which are updated in handle packet

    Args:
        protocol: Protocol for which statistics should be collected
        message: Contains the message information  
    """
    wrapper = _statistics_protocol_wrappers.get(protocol)
    if wrapper is not None:
        wrapper.update_tracked_variable_statistic(
            protocol.provider.current_time(), protocol.provider.tracked_variables
        )
    return DispatchReturn.CONTINUE


class StatisticsProtocolWrapper:
    """'
    Do not use this class directly, instead use
    [create_statistics][gradys_embedded.protocol.plugin.statistics.create_statistics].

    Wraps the protocol's calls into a call chain. Instead of going directly to the protocol's methods calls to the
    protocol interface will be passed down a chain of registered handlers. The protocol's own method is at the end
    of the chain.
    """

    _statistics_time_list: List[Dict[str, Any]]
    _statistics_tracked_variables_list: List[Dict[str, Any]]
    _statistics_collection_interval: float

    def __init__(self, protocol: IProtocol, file_name_part: str, collection_interval: float,
                 output_dir: Optional[str] = None, flush_every: int = DATA_FLUSH_EVERY):
        """
        Instantiates a protocol wrapper. Should not be instantiated directly, create a statistics using the
        [create_statistics][gradys_embedded.protocol.plugin.statistics.create_statistics] method.

        **Do not instantiate this class directly**

        Args:
            protocol: Protocol whose calls will be wrapped
        """

        self._dispatcher = create_dispatcher(protocol)

        self._id = file_name_part
        self._statistics_time_list = []
        self._statistics_tracked_variables_list = []
        self._statistics_collection_interval = collection_interval

        # Where the CSVs land. None keeps the historical behaviour of writing
        # relative to the current working directory.
        self._output_dir = Path(output_dir) if output_dir is not None else None
        if self._output_dir is not None:
            self._output_dir.mkdir(parents=True, exist_ok=True)

        # Rows are held in memory and rewritten periodically. Without this the
        # files only appear in finish(), so anything short of a clean shutdown
        # loses the entire run.
        self._flush_every = flush_every
        self._samples_since_flush = 0

        protocol.provider.schedule_timer("statistics", protocol.provider.current_time() + self._statistics_collection_interval)

    def register(self):
        """
        Registers all the methods needed for collecting the statistics
        """

        # Simulation and real time
        self._dispatcher.register_handle_timer(handle_timer_srt)

        # Tracked variables
        self._dispatcher.register_handle_packet(handle_packet_tv)

    def unregister(self):
        """
        Unregisters all the methods needed for collecting the statistics
        """

        # Simulation and real time
        self._dispatcher.unregister_handle_timer(handle_timer_srt)

        # Tracked variables
        self._dispatcher.unregister_handle_packet(handle_packet_tv)

    def update_srt_statistic(self, simulation_time: float, real_time: float) -> None:
        """
        Updates the collected statistics for simulation and real time

        Args:
            simulation_time: Current simulation time
            real_time: Current real time
        """

        self._statistics_time_list.append(
            {"simulation_time": simulation_time, "real_time": real_time}
        )

        self._samples_since_flush += 1
        if self._flush_every and self._samples_since_flush >= self._flush_every:
            self.create_statistic_files()

    def update_tracked_variable_statistic(
        self, simulation_time: float, tracked_variables: Dict[str, Any]
    ):
        """
        Updates the collected statistics for tracked variables and the changes based at simulation time

        Args:
            simulation_time: Current simulation time
            tracked_variables: Dictionary containing
        """

        self._statistics_tracked_variables_list.append(
            {"simulation_time": simulation_time} | tracked_variables
        )

    def _path_for(self, prefix: str) -> str:
        protocol = self._dispatcher._protocol
        name = f"{prefix}_{self._id}_{type(protocol).__name__}_{protocol.provider.get_id()}.csv"
        if self._output_dir is not None:
            return str(self._output_dir / name)
        return name

    def create_statistic_files(self) -> None:
        """
        Creates files for the collected statistics.

        Safe to call repeatedly: each call rewrites both files in full from the
        rows collected so far. This is what makes a run survive an unclean
        shutdown, and it is called periodically as well as at finish.
        """

        pd.DataFrame(self._statistics_time_list).to_csv(self._path_for("simulation_real_time"))
        pd.DataFrame(self._statistics_tracked_variables_list).to_csv(self._path_for("tracked_variables"))
        self._samples_since_flush = 0


_statistics_protocol_wrappers: Dict[IProtocol, StatisticsProtocolWrapper] = {}


def create_statistics(protocol: IProtocol, file_name_part: str = "",
                      collection_interval: float = DATA_COLLECTION_INTERVAL,
                      output_dir: Optional[str] = None,
                      flush_every: int = DATA_FLUSH_EVERY) -> StatisticsProtocolWrapper:
    """
    Creates statistics which wraps a protocol instance and it's methods. Implements a call chain for each of the
    protocol interface's methods. The class returned from this function can be used to add functions to the call chain
    of those wrapped methods. The original method implementation is not lost.

    Is a protocol that was already wrapped is passed as an argument, return the wrapper for that protocol.

    Beware that this module uses monkey patching and may result in broken protocols if someone else tries to tamper with
    the protocol's methods.

    If you want to implement an plugin or some other behaviour that requires overriding protocol's
    methods you should use this function

    Args:
        protocol: Protocol being wrapped
        file_name_part: Interpolated into the middle of each CSV's name
        collection_interval: Seconds between samples
        output_dir: Directory to write the CSVs into. Defaults to the process's
            current working directory, which is the historical behaviour. A
            long-running service passes its per-run directory here.
        flush_every: Rewrite the CSVs every N samples. 0 disables periodic
            writes, restoring write-only-at-finish behaviour.

    Returns:
        StatisticsProtocolWrapper instance that allows methods to be added to the call chain
    """

    global _statistics_protocol_wrappers
    if protocol not in _statistics_protocol_wrappers:
        _statistics_protocol_wrappers[protocol] = StatisticsProtocolWrapper(
            protocol, file_name_part, collection_interval,
            output_dir=output_dir, flush_every=flush_every,
        )
        _statistics_protocol_wrappers[protocol].register()

    return _statistics_protocol_wrappers[protocol]


def finish_statistics(protocol: IProtocol) -> None:
    """
    Finishes the statistics: unregisters the handlers, writes the files, and
    forgets the protocol.

    Safe to call for a protocol that has no statistics, and safe to call twice —
    a mission stop can race with process shutdown, and neither should raise.

    Args:
        protocol: Protocol being wrapped
    """

    wrapper = _statistics_protocol_wrappers.pop(protocol, None)
    if wrapper is None:
        return

    try:
        wrapper.unregister()
    except ValueError:
        # Handlers were already removed; the files still need writing.
        pass

    wrapper.create_statistic_files()

    # Both registries are keyed by protocol instance and module-global. Left
    # alone, every mission in a long-running process leaks a wrapper and pins
    # its dead protocol in memory.
    dispose_dispatcher(protocol)
