from __future__ import annotations

import asyncio
import logging
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from gradys_embedded.runner.runner import EmbeddedRunner


class CommunicationBackend(ABC):
    """One inter-node message transport.

    A backend owns both sides of a single ``communication_protocol``:
      - the data-plane *serve* (receive) side, run by :meth:`serve` for the runner's lifetime;
      - the *send*/*broadcast* side, called by ``EmbeddedProvider.send_communication_command``.

    It is constructed with the :class:`EmbeddedRunner` (mirroring the message-app pattern) so it can
    reach ``runner._configuration``, ``runner._loop``, ``runner._session`` (shared aiohttp client),
    and ``runner._encapsulator`` (delivery target; ``None`` before the protocol starts).
    """

    def __init__(self, runner: "EmbeddedRunner") -> None:
        self._runner = runner
        self._configuration = runner._configuration
        self._logger = logging.getLogger(type(self).__module__)

    @property
    def _port(self) -> int:
        """This node's data-plane port from ``node_ip_dict``."""
        own_addr = self._configuration.node_ip_dict[self._configuration.node_id]
        return int(own_addr.rsplit(":", 1)[1])

    @abstractmethod
    async def serve(self) -> None:
        """Run the data plane (receive side). Awaits for the runner's lifetime."""

    @abstractmethod
    def send(self, dest_node_id: int, payload: dict) -> None:
        """Fire-and-forget unicast of ``payload`` to a single peer."""

    @abstractmethod
    def broadcast(self, payload: dict) -> None:
        """Fire-and-forget send of ``payload`` to every other peer."""

    async def close(self) -> None:
        """Release any held resources (sessions). Default: nothing."""
        return None

    def _fire_and_forget(self, coro) -> None:
        task = self._runner._loop.create_task(coro)
        task.add_done_callback(self._log_task_exception)

    def _log_task_exception(self, task: asyncio.Task) -> None:
        if not task.cancelled() and task.exception() is not None:
            self._logger.error(f"Fire-and-forget task failed: {task.exception()}")
