"""
RAFT Consensus Plugin for GrADyS-SIM

This module provides distributed consensus capabilities using the RAFT algorithm.
It enables protocols to reach agreement on shared values in a fault-tolerant manner.

Key Features:

- Fault-tolerant consensus with node failure handling

- Active node discovery for dynamic majority calculations

- Heartbeat-based failure detection

- Dual operation modes (Classic and Fault-Tolerant)

- Seamless integration with GrADyS-SIM protocols

- Consensus variables instead of traditional log replication

Example:

    from gradysim.protocol.plugin.raft import RaftConfig, RaftMode, RaftConsensusPlugin
    
    # Configure consensus
    config = RaftConfig()
    config.set_election_timeout(150, 300)
    config.set_heartbeat_interval(50)
    config.add_consensus_variable("sequence", int)
    config.set_raft_mode(RaftMode.FAULT_TOLERANT)
    
    # Initialize and start
    consensus = RaftConsensusPlugin(config=config, protocol=self)
    consensus.set_known_nodes([0, 1, 2, 3, 4])
    consensus.start()
    
    # Propose values (leader only)
    if consensus.is_leader():
        consensus.propose_value("sequence", 42)
"""

from .raft_config import RaftConfig, FailureConfig, RaftMode
from .raft_consensus import RaftConsensusPlugin
from .raft_state import RaftState


__all__ = [
    "RaftConfig",
    "FailureConfig",
    "RaftMode",
    "RaftConsensusPlugin",
    "RaftState"
]
