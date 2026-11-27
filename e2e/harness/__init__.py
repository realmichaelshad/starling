"""The end-to-end harness: local fakes for the two paid third parties, plus the real processes.

`python -m e2e.harness up --state e2e/.run/state.json` starts, in order:

1. a fake OpenAI server (the Chat Completions subset the services proxy uses),
2. a fake Stripe server (the API subset `billing/stripe_client.py` uses, plus hosted
   Checkout and Customer Portal pages that deliver signed webhooks),
3. the real services process (`python -m starling_server.services.main`) pointed at the
   fake OpenAI with `STARLING_WORLD_OFFLINE=true`,
4. the real gateway (`uvicorn starling_server.app:create_app --factory`, one worker).

Everything binds 127.0.0.1 on a free port chosen at runtime, writes its logs into the run
folder, and is stopped again by `down` (engine workers included).
"""
from .state import HarnessState, read_state, write_state  # noqa: F401
