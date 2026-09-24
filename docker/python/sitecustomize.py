"""Automatically loaded by Python's site module when /opt/senda is on PYTHONPATH."""
try:
    from senda_argus_bootstrap import bootstrap
    bootstrap()
except Exception:
    # Fail-open by design: Agent startup must not depend on audit availability.
    pass
