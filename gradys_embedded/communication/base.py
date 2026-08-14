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

    def __init__(self, runner: "EmbeddedRunner", configuration) -> None:
        self._runner = runner
        # The MISSION's context, never the provisioned config alone. The
        # transport and the peer map are mission-scoped and a backend is built
        # per mission; a fallback to runner._configuration would silently ignore
        # everything the mission supplied.
        self._configuration = configuration
        self._logger = logging.getLogger(type(self).__module__)
        # asyncio only holds a weak reference to a running task, so an
        # unreferenced fire-and-forget send can be garbage-collected before it
        # completes. Hold them until they finish.
        self._pending_tasks: set[asyncio.Task] = set()
        # Set by serve() once the listener is actually bound/open, so the runner
        # can await readiness instead of assuming the serve task got that far.
        self._ready = asyncio.Event()

    def _signal_ready(self) -> None:
        self._ready.set()

    async def wait_ready(self) -> None:
        """Blocks until :meth:`serve` has bound/opened its listener.

        Backends with no positive bind signal may set readiness after a short
        grace period instead; a failed bind kills the serve task, which the
        runner races against this wait.
        """
        await self._ready.wait()

    @property
    def _port(self) -> int:
        """This node's data-plane port: the provisioned ``data_port``.

        A property of the machine; it never changes with a mission -- only the
        transport serving it does.
        """
        return self._configuration.data_port

    @abstractmethod
    async def serve(self) -> None:
        """Run the data plane (receive side).

        Awaits until :meth:`close` releases it. The transport is a mission
        parameter, so this runs for the life of a *mission*, not the process --
        a mission selecting a different protocol stops this one and starts
        another on the same port.
        """

    @abstractmethod
    def send(self, dest_node_id: int, payload: dict) -> None:
        """Fire-and-forget unicast of ``payload`` to a single peer."""

    @abstractmethod
    def broadcast(self, payload: dict) -> None:
        """Fire-and-forget send of ``payload`` to every other peer."""

    async def close(self) -> None:
        """Stop serving and release everything held, including the listener.

        Must be safe to call whether or not :meth:`serve` ever ran, and must
        unblock a running `serve()` so the port is free for the next mission's
        transport. Callers still cancel the serve task afterwards as a backstop.
        """
        return None

    def _fire_and_forget(self, coro) -> None:
        task = self._runner._loop.create_task(coro)
        self._pending_tasks.add(task)
        task.add_done_callback(self._pending_tasks.discard)
        task.add_done_callback(self._log_task_exception)

    def _log_task_exception(self, task: asyncio.Task) -> None:
        if not task.cancelled() and task.exception() is not None:
            self._logger.error(f"Fire-and-forget task failed: {task.exception()}")
