.. _adr-0005:

================================
ADR-0005: No persistence in v0.1
================================

Status
    Accepted (v0.1)

Context
-------

The conversation has to survive *something*. The options range from a single
JSON file to a SQLite database with migrations, encryption and retention
policies.

Options considered
------------------

.. list-table::
   :header-rows: 1
   :widths: 28 36 36

   * - Option
     - Strengths
     - Cost
   * - **In-memory only**
     - Zero schema, zero migration, zero privacy surface, trivially testable
     - Conversation is lost on restart
   * - Single JSON file per session
     - History across restarts
     - Retention, deletion, corruption handling, and "which file is the current
       conversation?"
   * - SQLite
     - Queryable history, durable
     - Schema and migrations, a DB file to secure and to delete correctly
   * - Cloud sync
     - Convenience
     - Out of scope for a local-first private product

Decision
--------

v0.1 keeps the session in memory. Conversation state lives in
:class:`~quantsmind_agent.session.Session` and is discarded when the application
closes. The only persisted state is *settings* (``config.toml``), the *last
workspace path*, and the *log file*.

Consequences
------------

* The privacy statement is verifiable rather than promised: conversation text is
  in memory and in the log directory; the only data sent anywhere is the model
  request the user asked for.
* No database dependency, no migration story, no schema to keep compatible.
* "New conversation" is instant and total.
* Losing history across restarts is a real limitation, and it is documented in
  :doc:`../../releases/v0.1` rather than papered over.
* Debugging is unaffected: ``Session.to_dict`` yields a JSON snapshot, and the
  log records each turn's outcome.

Future persistence
------------------

v0.2 will add opt-in history, and the shape is already implied by the code:
:meth:`Session.to_dict` is the serialisation, :class:`~quantsmind_agent.schema.Message`
is the record, and ``TurnRecord`` is the summary to show in a list. The decision
then is not *what shape* but *where it lives and how it is deleted* -- a question
worth answering with the user's consent in view.

Rejected alternative, restated
------------------------------

Persisting conversations in v0.1 would have created a security obligation
(stored user data that must be protected and purged) in exchange for convenience
that a developer-focused tool can live without for one release.