"""Automated reference testing of the ORIGINAL Webbed (state-aware runner around the read-only probe).

Modules
  state      classification of probe rows: readiness, attachment, consistency (pure functions)
  bindings   action -> key bindings, and how each action shows up in the game's own input object (oIO)
  capture    SessionWriter: writes sessions in exactly the format of webbed_probe.py (segment_NN.jsonl ...)
  driver     input drivers (external Windows SendInput; never memory writes or injection) + safety guard
  provider   LiveProvider: frame-synchronised state from webbed_probe.Game/Recorder (read-only)
  mock       MockGame + MockInputDriver: a fake game for unit tests (NOT the recovered physics)
  scenarios  scripted scenarios (M2, W1-W5) as per-frame generators with state-aware waits
  runner     orchestration: readiness, record, quality gate, failure preservation, retries, report
  testrun    run the unittest suite and return per-test outcomes as JSON

Nothing here writes to the game process. Inputs go through the OS input queue only while the game window
is in the foreground; every abort path releases all held inputs.
"""
