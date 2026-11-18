"""Engineering ChangeSets: lifecycle, operations, transactional apply and recovery.

The engine implements QM-P09-CS-001. Kept separate from
:mod:`quantsmind_agent.services.tasks` on purpose -- a ChangeSet is a transaction
over files with its own state machine, and a task is one possible reason for one.
"""
