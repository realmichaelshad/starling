"""What an excellent ghostwriter would produce for each evaluation press, in that customer's voice.

Two uses. Read beside a run's report, they are the bar a real model's drafts are judged against.
Served by the stand-in (the script hook in e2e/harness/fake_openai.py), they test the other half of
quality, the half the product controls: given a great answer, does the engine keep it intact, ship
it fast and show it with its sources, or does a check tuned for a weaker model rewrite, block or
wait on it?

Rules the answers follow, because the engine enforces them and a real model is told them: no
figure, date or event that the post, its links or the facts do not give; no personal experience the
writer has not written about; nothing copied from the post or from the writer's own samples; one
point per reply; the writer's own case, length, punctuation and emoji habits.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Golden:
    verdict: str            # "act" or "wait"
    text: str               # the draft or the answer ("" for a wait)
    point: str = ""         # the one thing a reply adds
    why: str = ""           # for a wait, the card's reason


GOLDEN: dict[str, Golden] = {
    # ------------------------------------------------------------------ Maya, terse ML engineer
    "terse-agree": Golden("act", "nice jump. now check the questions whose answer spans two chunks, that's where small "
                                 "chunks quietly lose",
                          point="small chunks hurt answers that span two of them"),
    "terse-contest": Golden("act", "a million tokens on every call is a very expensive way to avoid fine-tuning. "
                                   "latency agrees",
                            point="long context costs money and latency on every call"),
    "terse-question": Golden("act", "p95 latency. and a weekly read of real inputs, which catches what no metric does",
                             point="p95 plus reading real inputs"),
    "terse-claim": Golden("act", "most of this lands on model providers, not on teams calling an api. check which one "
                                 "you are before you panic",
                          point="the obligations fall on providers, not API users"),
    # The link presses use only the linked page's own figures (e2e/eval/world_fixtures.json).
    "terse-link": Golden("act", "the 12% is the number. tokens got 38% cheaper and tasks still cost more, because they "
                                "use 1.8x the tokens. price the task, not the token",
                         point="compare the cost of a finished task, not the price of a token"),
    "terse-offbeat": Golden("act", "congrats. a first marathon is no joke, enjoy the stairs tomorrow",
                            point="a warm, brief congratulation"),
    "terse-create-post": Golden("act", "prompt caching is the cheapest latency win nobody benchmarks. same model, same "
                                       "answers, faster first token"),
    "terse-create-reply": Golden("act", "worth splitting your eval by question type. the ones that need two chunks are "
                                        "where 300 tokens starts hurting",
                                 point="split the eval by questions that span chunks"),
    "terse-enhance": Golden("act", "most agents are a script with extra steps. they cost more and break more"),
    "terse-chat": Golden("act", "Three that fit you today:\n- prompt caching: what it really saves you in time to first "
                                "token\n- how your eval set leaks into your prompts, and how you rotate it\n- the last "
                                "prod incident you had that turned out to be a timeout"),

    # ------------------------------------------------------------------ Daniel, SaaS founder
    "essayist-agree": Golden("act", "The customers you lose on a price rise are rarely the ones you'd have kept anyway. "
                                    "The more useful number is who signs up next.",
                             point="watch who signs up after the rise, not who left"),
    "essayist-contest": Golden("act", "Freemium isn't dead, it's expensive. If a product can't show its value before a "
                                      "sales call, a free tier won't save it. If it can, the free tier is the cheapest "
                                      "salesperson you'll ever hire.",
                               point="freemium works when the product shows its own value"),
    "essayist-question": Golden("act", "I'd argue for support. Whoever answers the tickets learns where the product "
                                       "breaks faster than any roadmap review.",
                                point="support learns the product's weak spots first"),
    "essayist-claim": Golden("act", "Accepting a currency is the easy half. Pricing in it, and deciding who absorbs the "
                                    "conversion, is where small SaaS teams quietly lose margin.",
                             point="pricing and conversion costs are the hard part"),
    "essayist-link": Golden("act", "The figure I'd underline is the 9%. Only that share of pages said what happens "
                                   "when a customer outgrows their tier, and those pages got the fewest billing "
                                   "questions. Most pricing pages explain how the relationship starts and go quiet "
                                   "about the part that decides whether it lasts.",
                            point="say on the page what happens when a customer outgrows their tier"),
    "essayist-lowvalue": Golden("wait", "", why="There is nothing to add to a gm."),
    "essayist-create-post": Golden("act", "Raising prices is mostly a copywriting problem. Customers rarely leave over "
                                          "the new number; they leave because nobody explained what they get for it."),
    "essayist-create-reply": Golden("act", "Losing a handful of customers to a price rise is usually the price working. "
                                           "It filters for the teams who get real value from the product.",
                                    point="a price rise filters for the right customers"),
    "essayist-enhance": Golden("act", "We stopped offering custom contracts to small customers. Sales got faster, and "
                                      "nobody we cared about left. More founders should say no to custom deals."),
    "essayist-chat": Golden("act", "A few that suit you this week:\n- the email you send before a price rise, and what "
                                   "to put in it\n- the one support ticket that changed your roadmap\n- why annual plans "
                                   "are a commitment device, not a discount"),

    # ------------------------------------------------------------------ Rosa, strength coach
    "coach-agree": Golden("act", "Full body three times a week means you practise every lift three times instead of "
                                 "once. That's the whole trick 💪",
                          point="frequency: each lift practised three times a week"),
    "coach-contest": Golden("act", "Your metabolism doesn't care about the clock. What you eat across the whole day "
                                   "matters far more than how often. Eat when it suits you.",
                            point="daily totals beat meal timing"),
    "coach-question": Golden("act", "Most people who lift do well with a palm-sized portion of protein at every meal. "
                                    "Get that right every day before you worry about the exact number.",
                             point="a simple habit before an exact number"),
    "coach-claim": Golden("act", "And walking counts. A brisk walk most days gets most people there without setting "
                                 "foot in a gym.",
                          point="walking alone meets the recommendation"),
    "coach-link": Golden("act", "Read this before you buy another shaker 😅 32 trials, one answer: hit your daily "
                                "total first, around 1.6 g per kg of body weight. Spread it over three or four meals "
                                "if that's easy. The post-workout window made no measurable difference.",
                         point="the daily total first, timing last"),
    "coach-offbeat": Golden("wait", "", why="Rust async is outside what you post about."),
    "coach-create-post": Golden("act", "Your first months in the gym: learn a handful of lifts, eat enough protein, "
                                       "sleep. That's it. The fancy stuff can wait 💪"),
    "coach-create-reply": Golden("act", "This is the most common fix I'd give anyone stuck on a split. More practice per "
                                        "lift beats more exercises per day.",
                                 point="practice per lift beats volume per day"),
    "coach-enhance": Golden("act", "New to the gym? Don't copy influencer programs. Start with three full-body days a "
                                   "week and get stronger slowly 💪"),
    "coach-chat": Golden("act", "Three for today:\n- the meal-timing myth, and what actually matters\n- a 20-minute "
                                "workout for busy weeks\n- why beginners should film their squats before changing them"),

    # ------------------------------------------------------------------ a brand-new account
    "new-star": Golden("act", "Worth checking how the smaller chunks do on questions whose answer spans more than one "
                              "of them.", point="check multi-chunk questions"),
    "new-create-post": Golden("act", "The boring fix is usually the right one. It just never makes a good demo."),
    "new-chat": Golden("act", "I don't know your writing yet, so these are general. On your own X profile, open "
                              "Settings in this panel and press \"Add my posts from this page\", and I'll match your "
                              "voice and your topics."),
}

# What an excellent model would conclude about each account from its writing: its beats, and the
# voice card a drafting model should be given. The engine's own reading is compared with these.
VOICE_CARDS: dict[str, dict[str, object]] = {
    "terse": {
        "beats": ["LLM evaluation", "inference latency and cost", "running LLMs in production"],
        "card": "Maya writes like a senior engineer thinking out loud: all lowercase, one or two short sentences, "
                "dry and slightly contrarian. She is skeptical of hype (agents, benchmarks, giant context windows) and "
                "believes in evals, measuring p95 latency, caching and boring fixes. She makes a point with a concrete "
                "engineering detail, never with a question, rarely with a number, no emoji, no hashtags, no "
                "exclamation marks.",
    },
    "essayist": {
        "beats": ["SaaS pricing", "churn and retention", "bootstrapping a profitable company"],
        "card": "Daniel writes in calm, complete, capitalised sentences, usually one to three, sometimes a short "
                "numbered list. He is a bootstrapped B2B founder who argues from customers and incentives: pricing "
                "clarity, support as research, saying no. He reframes a claim (\"X isn't a discount, it's a "
                "commitment device\"), is measured rather than combative, and never uses emoji or hype.",
    },
    "coach": {
        "beats": ["strength training for beginners", "nutrition and protein", "fitness myths"],
        "card": "Rosa is an upbeat, practical strength coach who talks straight to the reader (\"you\"). Short punchy "
                "sentences, plain words, one clear action, the occasional emoji (💪, 🙃, ☕). She busts myths kindly, "
                "values consistency, sleep, protein and walking, and never shames anyone.",
    },
}
