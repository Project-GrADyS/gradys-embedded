"""
Raft Consensus Main Interface

Provides the main interface for Raft consensus using the Facade pattern.
This module exposes a simple, clean API that hides the complexity of the
underlying Raft implementation while providing all necessary functionality.

The RaftConsensus class serves as the main entry point for users who want
to integrate Raft consensus into their Gradysim protocols.
"""

import logging
from typing import Any, Callable, Dict, List, Optional, Set, Type

from .raft_config import RaftConfig
from .raft_node import RaftNode
from .raft_state import RaftState
from ..dispatcher import create_dispatcher, DispatchReturn
from ...messages.communication import CommunicationCommand, CommunicationCommandType
from ...interface import IProtocol

# This prefix is used to avoid conflicts with other plugins.
RAFT_DISPATCH_PREFIX = "__RAFT__"

class RaftConsensusPlugin:
    """
    Main interface for Raft consensus implementation.
    
    Provides a simple, clean API for integrating Raft consensus into
    Gradysim protocols. This class implements the Facade pattern to
    hide the complexity of the underlying Raft implementation.
    
    Main Methods:
        - start() / stop(): Control consensus lifecycle
        - propose_value(): Propose new values (leader only)
        - get_committed_value() / get_all_committed_values(): Retrieve consensus values
        - is_leader() / get_leader_id(): Check leadership status
        - get_current_term() / get_current_state(): Get consensus state
        - handle_message() / handle_timer(): Process Raft protocol events
        - set_known_nodes(): Configure cluster membership
        - get_statistics() / get_state_info(): Get debugging information
    
    Example:
        # 1. Configure consensus
        config = RaftConfig()
        config.set_election_timeout(150, 300)  # 150-300ms election timeout
        config.set_heartbeat_interval(50)      # 50ms heartbeat interval
        config.add_consensus_variable("sequence", int)
        config.add_consensus_variable("leader_position", str)
        config.set_logging(enable=True, level="INFO")
        
        # 2. Create consensus instance (simplified)
        consensus = RaftConsensus(config=config, protocol=protocol)
        
        # 3. Set known nodes and start consensus
        consensus.set_known_nodes([1, 2, 3, 4, 5])
        consensus.start()
        
        # 4. Propose values (only works if this node is leader)
        if consensus.is_leader():
            consensus.propose_value("sequence", 42)
            consensus.propose_value("leader_position", "north")
        
        # 5. Get committed values
        sequence_value = consensus.get_committed_value("sequence")
        position_value = consensus.get_committed_value("leader_position")
        
        # 6. Get all committed values
        all_values = consensus.get_all_committed_values()
        
        # 7. Check consensus state
        is_leader = consensus.is_leader()
        leader_id = consensus.get_leader_id()
        current_term = consensus.get_current_term()
        current_state = consensus.get_current_state()
        
        # 8. Handle messages and timers (call these from your protocol)
        consensus.handle_message(message_str)
        consensus.handle_timer("heartbeat")
        consensus.handle_timer("election")
        
        # 9. Get statistics and information
        stats = consensus.get_statistics()
        state_info = consensus.get_state_info()
        config_info = consensus.get_configuration()
        
        # 10. Check if system is ready
        if consensus.is_ready():
            print("Consensus system is ready")
        
        # 11. Check failure detection (if enabled)
        failed_nodes = consensus.get_failed_nodes()
        active_nodes = consensus.get_active_nodes()
        if consensus.is_node_failed(3):
            print("Node 3 is currently failed")
        
        # 12. Stop consensus when done
        consensus.stop()
    """
    
    def __init__(self, config: RaftConfig, protocol: IProtocol):
        """Initialize Raft consensus plugin with Gradysim protocol provider."""
        errors = config.validate()
        if errors:
            raise ValueError(f"Invalid configuration: {'; '.join(errors)}")

        self.config = config
        self._protocol = protocol
        self._provider = protocol.provider
        self._dispatch_header = f"{RAFT_DISPATCH_PREFIX}:"

        self._validate_provider()

        self._get_node_id_callback: Optional[Callable[[], int]] = getattr(self._provider, "get_id", None)
        if self._get_node_id_callback is None:
            raise ValueError("Protocol provider must implement get_id() for RaftConsensusPlugin")

        try:
            self.node_id = self._get_node_id_callback()
        except Exception as exc:
            raise ValueError(f"Failed to get node ID from provider: {exc}") from exc

        self.logger = logging.getLogger(f"RaftConsensus-{self.node_id}")
        if config._enable_logging:
            self.logger.setLevel(getattr(logging, config._log_level))

        callbacks = self._build_callbacks()

        self._raft_node = RaftNode(
            node_id=self.node_id,
            config=config,
            callbacks=callbacks
        )

        self._dispatcher = create_dispatcher(protocol)

        self.configure_handle_message()
        self.configure_handle_timer()

        self.logger.info(f"RaftConsensus initialized for node {self.node_id}")

    def _validate_provider(self) -> None:
        """Ensure the protocol provider exposes the expected Gradysim hooks."""
        required_methods = ["send_communication_command", "schedule_timer", "cancel_timer", "current_time"]
        missing = [name for name in required_methods if not callable(getattr(self._provider, name, None))]
        if missing:
            raise ValueError(f"Protocol provider missing required methods: {missing}")

    def _build_callbacks(self) -> Dict[str, Callable[..., Any]]:
        """Create callbacks consumed by the internal Raft node."""
        return {
            "send_message_callback": self._send_message,
            "send_broadcast_callback": self._send_broadcast,
            "schedule_timer_callback": self._schedule_timer,
            "cancel_timer_callback": self._cancel_timer,
            "get_current_time_callback": self._get_current_time
        }

    def _add_dispatch_prefix(self, value: str) -> str:
        """Attach the RAFT dispatcher prefix to timer and message identifiers."""
        value_str = str(value)
        if value_str.startswith(self._dispatch_header):
            return value_str
        return f"{self._dispatch_header}{value_str}"

    def _strip_dispatch_prefix(self, value: str) -> Optional[str]:
        """Remove the RAFT dispatcher prefix; return None if it is not present."""
        if not isinstance(value, str) or not value.startswith(self._dispatch_header):
            return None
        return value[len(self._dispatch_header):]

    def _send_message(self, message: str, target_id: int) -> None:
        """Send point-to-point RAFT messages through the provider."""
        try:
            payload = self._add_dispatch_prefix(message)
            command = CommunicationCommand(CommunicationCommandType.SEND, payload, target_id)
            self._provider.send_communication_command(command)
        except Exception as exc:
            self.logger.error("Error sending RAFT message to node %s: %s", target_id, exc)

    def _send_broadcast(self, message: str) -> None:
        """Broadcast RAFT messages through the provider."""
        try:
            payload = self._add_dispatch_prefix(message)
            command = CommunicationCommand(CommunicationCommandType.BROADCAST, payload)
            self._provider.send_communication_command(command)
        except Exception as exc:
            self.logger.error("Error broadcasting RAFT message: %s", exc)

    def _schedule_timer(self, timer_name: str, delay_ms: int) -> None:
        """Schedule RAFT timers using Gradysim's timing service."""
        prefixed_timer = self._add_dispatch_prefix(timer_name)
        try:
            delay_seconds = delay_ms / 1000.0
            absolute_time = self._provider.current_time() + delay_seconds
            self._provider.schedule_timer(prefixed_timer, absolute_time)
        except Exception as exc:
            self.logger.error("Error scheduling RAFT timer '%s': %s", timer_name, exc)

    def _cancel_timer(self, timer_name: str) -> None:
        """Cancel RAFT timers previously scheduled through the provider."""
        prefixed_timer = self._add_dispatch_prefix(timer_name)
        try:
            self._provider.cancel_timer(prefixed_timer)
        except Exception as exc:
            self.logger.error("Error canceling RAFT timer '%s': %s", timer_name, exc)

    def _get_current_time(self) -> float:
        """Expose the provider current time as required by the Raft node."""
        try:
            return float(self._provider.current_time())
        except Exception as exc:
            raise RuntimeError(f"Failed to get simulation time: {exc}") from exc

    def get_node_id(self) -> int:
        """
        Get the current node ID.
        
        If a get_node_id_callback was provided during initialization,
        this method will call it to get the current node ID dynamically.
        Otherwise, it returns the static node_id.
        
        Returns:
            Current node ID
        """
        if self._get_node_id_callback is not None:
            try:
                return self._get_node_id_callback()
            except Exception as e:
                self.logger.error(f"Error getting node ID from callback: {e}")
                return self.node_id  # Fallback to stored node_id
        return self.node_id
    
    def start(self) -> None:
        """
        Start the consensus process.
        
        This method initializes the Raft node and begins the election timeout.
        The node will start as a follower and may become a candidate if no
        leader is discovered.
        """
        self.logger.info(f"Starting Raft consensus for node {self.node_id}")
        self._raft_node.start()
    
    def stop(self) -> None:
        """
        Stop the consensus process.
        
        This method stops the Raft node and cancels all active timers.
        """
        # Don't log here since RaftNode will log the stop
        self._raft_node.stop()
    
    def propose_value(self, variable_name: str, value: Any) -> bool:
        """
        Propose a new value for consensus.
        
        This method can only be called by the current leader. If this node
        is not the leader, the proposal will be rejected.
        
        Args:
            variable_name: Name of the consensus variable
            value: Value to propose
            
        Returns:
            True if proposal was accepted, False otherwise
            
        Raises:
            ValueError: If variable is not configured or value type is invalid
        """
        return self._raft_node.propose_value(variable_name, value)
    
    def get_committed_value(self, variable_name: str) -> Optional[Any]:
        """
        Get the committed value for a consensus variable.
        
        Args:
            variable_name: Name of the consensus variable
            
        Returns:
            Committed value, or None if not available
            
        Raises:
            ValueError: If variable is not configured
        """
        if not self.config.has_consensus_variable(variable_name):
            raise ValueError(f"Consensus variable '{variable_name}' not configured")
        
        return self._raft_node.get_committed_value(variable_name)
    
    def get_all_committed_values(self) -> Dict[str, Any]:
        """
        Get all committed consensus values.
        
        Returns:
            Dictionary of all committed values
        """
        return self._raft_node.get_all_committed_values()
    
    def is_leader(self) -> bool:
        """
        Check if this node is the current leader.
        
        Returns:
            True if this node is the leader, False otherwise
        """
        return self._raft_node.is_leader()
    
    def get_leader_id(self) -> Optional[int]:
        """
        Get the current leader ID.
        
        Returns:
            ID of the current leader, or None if no leader is known
        """
        return self._raft_node.get_leader_id()
    
    def get_current_term(self) -> int:
        """
        Get the current term.
        
        Returns:
            Current term number
        """
        return self._raft_node.get_current_term()
    
    def get_current_state(self) -> RaftState:
        """
        Get the current state of this node.
        
        Returns:
            Current Raft state (FOLLOWER, CANDIDATE, or LEADER)
        """
        return self._raft_node.state
    
    def configure_handle_message(self) -> None:
        """Intercept RAFT packets and route them to the internal node."""
        def handle_message(_instance: IProtocol, message_str: str) -> DispatchReturn:
            payload = self._strip_dispatch_prefix(message_str)
            if payload is None:
                return DispatchReturn.CONTINUE

            import json

            sender_id = 0
            try:
                data = json.loads(payload)
                sender_id = data.get("sender_id", 0)
            except (json.JSONDecodeError, TypeError, AttributeError):
                pass

            self._raft_node.handle_message(payload, sender_id)
            return DispatchReturn.INTERRUPT

        self._dispatcher.register_handle_packet(handle_message)

    def send_broadcast(self, message: str) -> None:
        """
        Send broadcast message to all nodes.
        
        This method sends a message to all nodes in the cluster using
        broadcast communication if available.
        
        Args:
            message: Message content to broadcast
        """
        if hasattr(self._raft_node, '_send_broadcast') and self._raft_node._send_broadcast is not None:
            self._raft_node._send_broadcast(message)
        else:
            self.logger.warning("Broadcast not available, falling back to individual messages")
            # Fallback to individual messages if broadcast not available
            for node_id in getattr(self._raft_node, '_known_nodes', []):
                if node_id != self.node_id:
                    self._raft_node._send_message(message, node_id)
    
    def configure_handle_timer(self) -> None:
        """Intercept RAFT timers created by the plugin."""
        def handle_timer(_instance: IProtocol, timer_name: str) -> DispatchReturn:
            payload = self._strip_dispatch_prefix(timer_name)
            if payload is None:
                return DispatchReturn.CONTINUE

            self._raft_node.handle_timer(payload)
            return DispatchReturn.INTERRUPT
        self._dispatcher.register_handle_timer(handle_timer)

    def set_known_nodes(self, node_ids: List[int]) -> None:
        """
        Set the list of known node IDs.

        This method should be called to inform the consensus system about
        all nodes in the cluster. This information is used for sending
        messages during elections and heartbeats.

        Args:
            node_ids: List of all node IDs in the cluster
        """
        self._raft_node.set_known_nodes(node_ids)

        detector = getattr(self._raft_node, '_heartbeat_detector', None)
        if detector and hasattr(self._provider, 'set_failure_detector'):
            try:
                self._provider.set_failure_detector(detector)
                self.logger.info('Connected failure detector to provider for connectivity-based failure detection')
            except Exception as exc:
                self.logger.warning('Failed to connect failure detector via provider: %s', exc)

        self.logger.info(f"Set known nodes: {node_ids}")

    def get_state_info(self) -> Dict[str, Any]:
        """
        Get current state information for debugging.
        
        Returns:
            Dictionary with current state information
        """
        return self._raft_node.get_state_info()
    
    def get_consensus_variables(self) -> Dict[str, Type]:
        """
        Get all configured consensus variables.
        
        Returns:
            Dictionary mapping variable names to their types
        """
        return self.config.get_consensus_variables()
    
    def has_consensus_variable(self, variable_name: str) -> bool:
        """
        Check if a consensus variable is configured.
        
        Args:
            variable_name: Name of the consensus variable
            
        Returns:
            True if the variable is configured, False otherwise
        """
        return self.config.has_consensus_variable(variable_name)
    
    def get_consensus_variable_type(self, variable_name: str) -> Optional[Type]:
        """
        Get the type of a consensus variable.
        
        Args:
            variable_name: Name of the consensus variable
            
        Returns:
            Type of the variable, or None if not found
        """
        return self.config.get_consensus_variable_type(variable_name)
    
    def get_configuration(self) -> Dict[str, Any]:
        """
        Get the current configuration.
        
        Returns:
            Dictionary representation of the configuration
        """
        return self.config.to_dict()
    
    def is_ready(self) -> bool:
        """
        Check if the consensus system is ready.
        
        Returns:
            True if the system is ready, False otherwise
        """
        return hasattr(self._raft_node, '_known_nodes') and self._raft_node._known_nodes is not None
    
    def get_statistics(self) -> Dict[str, Any]:
        """
        Get consensus statistics.
        
        Returns:
            Dictionary with consensus statistics
        """
        state_info = self.get_state_info()
        return {
            "node_id": self.node_id,
            "current_term": state_info["current_term"],
            "current_state": state_info["state"],
            "is_leader": self.is_leader(),
            "leader_id": self.get_leader_id(),
            "consensus_variables": list(self.get_consensus_variables().keys()),
            "committed_values_count": len(state_info["committed_values"]),
            "active_timers_count": len(state_info["active_timers"])
        }
    
    def get_simulation_active_nodes(self) -> Set[int]:
        """
        Get the set of nodes that are active in simulation.
        This is based on manual control (active/inactive state).
        
        Returns:
            Set of simulation active node IDs
        """
        return self._raft_node.get_simulation_active_nodes()
    
    def get_communication_failed_nodes(self) -> Set[int]:
        """
        Get the set of nodes that have communication failures.
        This is based on heartbeat detection.
        
        Returns:
            Set of communication failed node IDs
        """
        return self._raft_node.get_communication_failed_nodes()
    
    def get_communication_active_nodes(self) -> Set[int]:
        """
        Get the set of nodes that have active communication.
        This is based on heartbeat detection.
        
        Returns:
            Set of communication active node IDs
        """
        return self._raft_node.get_communication_active_nodes()
    
    def get_failed_nodes(self) -> Set[int]:
        """
        Get the set of currently failed nodes.
        DEPRECATED: Use get_communication_failed_nodes() instead.
        
        Returns:
            Set of failed node IDs, empty if failure detection is disabled
        """
        return self._raft_node.get_failed_nodes()
    
    def get_active_nodes(self) -> Set[int]:
        """
        Get the set of currently active nodes.
        DEPRECATED: Use get_simulation_active_nodes() or get_communication_active_nodes() instead.
        
        Returns:
            Set of active node IDs, empty if failure detection is disabled
        """
        return self._raft_node.get_active_nodes()
    
    def get_active_nodes_info(self) -> Dict[str, Any]:
        """
        Get detailed information about active nodes from this node's perspective.
        This method provides comprehensive information about cluster state and active nodes.
        
        Works in both CLASSIC and FAULT_TOLERANT modes with appropriate behavior for each:
        
        **CLASSIC mode:**

        - All known nodes are considered active (no failure detection)

        - Returns information based on the complete known node list
        
        **FAULT_TOLERANT mode:**

        - Uses actual failure detection to determine active nodes

        - Information accuracy differs by node role:

          * Leader: Complete and accurate active nodes information from heartbeat detection
          
          * Follower/Candidate: Active count from leader + limited local node knowledge
        
        Can be called on any node (leader, candidate, or follower) in any mode.
        Use this method to get detailed monitoring information about which nodes are active, 
        failed, and the current majority status from the perspective of the calling node.
        
        Returns:
            Dictionary containing:
            - 'active_nodes': List of active node IDs (sorted)
            - 'active_count': Number of active nodes
            - 'total_known': Total number of known nodes
            - 'majority_threshold': Current majority threshold
            - 'has_majority': Whether cluster has majority
            - 'detection_method': How active nodes were determined
                * 'classic_mode_all_active': All nodes active (Classic mode)
                * 'leader_heartbeat_detection': Leader using heartbeat detector
                * 'leader_shared_complete_list': Follower using complete list from leader
                * 'leader_shared_count_only': Follower using count from leader (limited IDs)
                * 'follower_local_detection': Follower using local detection (fallback)
            - 'last_update': Timestamp of last update (if available)
            - 'node_role': Role of this node ('leader', 'candidate', 'follower')
            - 'is_leader': Whether this node is the leader
            - 'leader_id': ID of the current leader (if known)
            - 'current_node_id': ID of this node
            - 'current_term': Current Raft term
            - 'raft_mode': Current Raft operation mode ('classic' or 'fault_tolerant')
            - 'failed_nodes': List of failed node IDs (empty in Classic mode)
            - 'failed_count': Number of failed nodes (0 in Classic mode)
            - 'detection_summary': Detailed detection info (if available)
            
        Example:
            ```python
            # Works in both modes
            active_info = consensus.get_active_nodes_info()
            print(f"Node {active_info['current_node_id']} role: {active_info['node_role']}")
            print(f"Active nodes: {active_info['active_nodes']}")
            print(f"Failed nodes: {active_info['failed_nodes']}")
            print(f"Has majority: {active_info['has_majority']}")
            print(f"Detection method: {active_info['detection_method']}")
            print(f"Raft mode: {active_info['raft_mode']}")
            
            if active_info['is_leader']:
                print("This node is the leader")
            elif active_info['leader_id']:
                print(f"Leader is node {active_info['leader_id']}")
            else:
                print("No current leader")
            ```
        """
        return self._raft_node.get_active_nodes_info()
    
    def has_quorum(self) -> bool:
        """
        Check if the system has enough active nodes to form a quorum.
        
        Returns:
            True if there are enough active nodes to operate, False otherwise
        """
        return self._raft_node.has_quorum()
    
    def has_majority_votes(self) -> bool:
        """
        Check if this node has received majority of votes in current election.
        
        Returns:
            True if majority of active nodes have voted for this node, False otherwise
        """
        return self._raft_node.has_majority_votes()
    
    def has_majority_confirmation(self) -> bool:
        """
        Check if majority of active nodes have confirmed current values.
        
        Returns:
            True if majority of active nodes have confirmed, False otherwise
        """
        return self._raft_node.has_majority_confirmation()
    
    def get_majority_info(self) -> Dict[str, Any]:
        """
        Get detailed information about majority status.
        
        Returns:
            Dictionary with majority information including active nodes, 
            majority threshold, and current status
        """
        return self._raft_node.get_majority_info()
    
    def is_node_failed(self, node_id: int) -> bool:
        """
        Check if a specific node is currently failed.
        
        Args:
            node_id: ID of the node to check
            
        Returns:
            True if the node is failed, False otherwise
        """
        return self._raft_node.is_node_failed(node_id)

    def is_simulation_active(self, node_id: int) -> bool:
        """
        Check if a specific node is currently active in simulation.
        This is the manual control state (active/inactive).
        
        Args:
            node_id: ID of the node to check
            
        Returns:
            True if the node is active in simulation, False otherwise
        """
        return self._raft_node.is_simulation_active(node_id)
    
    def is_communication_failed(self, node_id: int) -> bool:
        """
        Check if a specific node has communication failure.
        This is based on heartbeat detection.
        
        Args:
            node_id: ID of the node to check
            
        Returns:
            True if the node has communication failure, False otherwise
        """
        return self._raft_node.is_communication_failed(node_id)
    
    def get_is_active(self, node_id: int) -> bool:
        """
        Check if a specific node is currently active.
        DEPRECATED: Use is_simulation_active() or is_communication_failed() instead.
        
        Args:
            node_id: ID of the node to check
            
        Returns:
            True if the node is active, False otherwise
        """
        return self._raft_node.get_is_active(node_id)
    
    def set_simulation_active(self, node_id: int, active: bool) -> None:
        """
        Set this node's simulation active/inactive state.
        Only affects this node if node_id matches this node's ID.
        
        Args:
            node_id: ID of the node to set state
            active: True to make node active in simulation, False to make it inactive
        """
        self._raft_node.set_simulation_active(node_id, active)
    
    def set_is_active(self, node_id: int, active: bool) -> None:
        """
        Set this node's active/inactive state.
        DEPRECATED: Use set_simulation_active() instead.
        
        Args:
            node_id: ID of the node to set state
            active: True to make node active, False to make it inactive
        """
        self._raft_node.set_is_active(node_id, active)

    def get_failure_detection_metrics(self) -> Dict[str, Any]:
        """
        Get detailed metrics about failure detection performance.
        
        Returns:
            Dictionary with detailed failure detection metrics, or empty dict if not available
        """
        return self._raft_node.get_failure_detection_metrics()
    
    def set_cluster_id(self, cluster_id: Optional[int]) -> None:
        """
        Set the cluster ID for this node.
        
        Args:
            cluster_id: Cluster ID to assign to this node, or None to clear
        """
        self._raft_node.set_cluster_id(cluster_id)
    
    def get_cluster_id(self) -> Optional[int]:
        """
        Get the cluster ID for this node.
        
        Returns:
            Cluster ID, or None if not set
        """
        return self._raft_node.get_cluster_id()
    
    def is_in_same_cluster(self, other_node_id: int) -> bool:
        """
        Check if another node is in the same cluster.
        
        Args:
            other_node_id: ID of the other node to check
            
        Returns:
            True if both nodes are in the same cluster, False otherwise
        """
        return self._raft_node.is_in_same_cluster(other_node_id)

 