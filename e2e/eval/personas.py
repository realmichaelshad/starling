"""Who the evaluation drafts for, and what it asks them to draft.

Three customers with plainly different voices and beats, and one brand-new account with nothing
written yet. Every name, handle and post here is invented. Each press carries a short note on what
a good result looks like, so whoever reads the report can judge it at a glance: is it in their
voice, does it add one thing, did it decide sensibly, is it current.

Each press also says which WORLD TOOLS it should use (`expects`), and the report checks them:
"web_check" (a checkable public claim: a web search ran and the card links its sources), "link"
(the post links a page: the card cites that page) and "news" (a created post: the card cites the
story it is about). The linked pages and the news stories of a stand-in run come from
`world_fixtures.json` (STARLING_WORLD_FIXTURES, e2e only); a real run reads the real pages.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any


@dataclass(frozen=True)
class Post:
    author: str
    handle: str
    text: str
    minutes_ago: int
    views: int = 0
    likes: int = 0
    replies: int = 0
    reposts: int = 0
    reply_ages: tuple[float, ...] = ()
    links: tuple[str, ...] = ()    # the post's own links, as the extension reads them

    def context(self, now: datetime | None = None) -> dict[str, Any]:
        """The PostContext the extension sends for this post (extension/shared/types.ts)."""
        now = now or datetime.now(timezone.utc)
        created = now - timedelta(minutes=self.minutes_ago)
        context: dict[str, Any] = {
            "author": self.author,
            "author_handle": self.handle,
            "text": self.text,
            "post_type": "post",
            "visibility": "public",
            "post_created_at": created.isoformat().replace("+00:00", "Z"),
            "captured_at": now.isoformat().replace("+00:00", "Z"),
            "visible_metrics": {"views": self.views, "likes": self.likes, "replies": self.replies,
                                "reposts": self.reposts},
        }
        if self.reply_ages:
            context["thread_scanned"] = True
            context["visible_reply_count"] = self.replies
            context["visible_reply_ages_minutes"] = list(self.reply_ages)
        if self.links:
            context["links"] = list(self.links)
        return context


@dataclass(frozen=True)
class Press:
    id: str
    # "star" (✦ on a post), "create_post", "create_reply", "enhance", "chat"
    kind: str
    good: str                      # what a good result looks like
    post: Post | None = None
    draft_text: str = ""
    message: str = ""
    # The world tools this press should use: "web_check", "link", "news" (see the module docstring).
    expects: tuple[str, ...] = ()


@dataclass(frozen=True)
class Persona:
    key: str
    name: str
    about: str                     # for the report only
    background: str = ""           # an own-material "background" note (never reaches a prompt)
    samples: tuple[str, ...] = ()  # own-material "voice" samples: their own writing
    presses: tuple[Press, ...] = field(default_factory=tuple)


# --------------------------------------------------------------------------- the posts they meet

RAG_CHUNKS = Post("Priya Raman", "priyabuildsai",
                  "We moved our RAG pipeline from 1k-token chunks to 300-token chunks and recall on our eval set "
                  "went from 62% to 81%. Chunking is underrated.",
                  25, views=8400, likes=210, replies=31, reposts=18, reply_ages=(2, 4, 9, 13, 20))
FINETUNE_DEAD = Post("Jordan Ellis", "jellis_ml",
                     "Hot take: fine-tuning is dead. With million-token context windows you can just put everything "
                     "in the prompt.",
                     40, views=22000, likes=540, replies=140, reposts=61, reply_ages=(1, 3, 6, 8, 15))
ONE_METRIC = Post("Sam Okoye", "samokoye",
                  "What's the one metric you actually watch for LLM apps in production?",
                  12, views=3100, likes=45, replies=22, reposts=3, reply_ages=(1, 2, 5, 7))
EU_AI_ACT = Post("Policy Watch", "policywatch_eu",
                 "The EU AI Act's obligations for general-purpose AI models started applying in August 2025.",
                 120, views=15000, likes=320, replies=48, reposts=90)
MARATHON = Post("Chris Lane", "chrislaneruns",
                "Just finished my first marathon! 4:12, legs are destroyed but so happy 🎉",
                180, views=900, likes=60, replies=14, reposts=1)

PRICE_RISE = Post("Lena Fischer", "lenabootstraps",
                  "We just raised our prices by 40% and lost 3 customers out of 400. Should have done it a year ago.",
                  30, views=12000, likes=380, replies=44, reposts=35, reply_ages=(3, 6, 10, 18))
FREEMIUM_DEAD = Post("Marcus Webb", "marcuswebbvc",
                     "Freemium is dead for B2B. Sales-led or bust.",
                     60, views=30000, likes=610, replies=210, reposts=88, reply_ages=(1, 2, 4, 9, 12))
FIRST_HIRE = Post("Aisha Karim", "aishakarim",
                  "Founders: what's the first role you hired after yourself, and would you do it again?",
                  20, views=5000, likes=90, replies=60, reposts=6, reply_ages=(1, 3, 4, 8))
STRIPE_CURRENCIES = Post("Fintech Daily", "fintechdaily",
                         "Stripe now lets businesses accept payments in more than 135 currencies.",
                         90, views=9000, likes=150, replies=20, reposts=40)
GM = Post("Kyle", "kylegm", "gm", 5, views=300, likes=12, replies=4)

SPLIT_TO_FULL_BODY = Post("Ben Carter", "bencarterlifts",
                          "Tried a 5-day split for 3 months and gained almost nothing. Switched to full body 3x a week "
                          "and I'm finally progressing.",
                          35, views=6000, likes=150, replies=25, reposts=9, reply_ages=(2, 5, 11, 16))
EAT_EVERY_3H = Post("Fit Tips Daily", "fittipsdaily",
                    "You need to eat every 3 hours to keep your metabolism going.",
                    60, views=40000, likes=900, replies=300, reposts=120, reply_ages=(1, 1, 3, 5, 7))
HOW_MUCH_PROTEIN = Post("Mia Torres", "miatorresfit",
                        "How much protein do you actually need per day? Genuinely confused by all the numbers.",
                        15, views=2500, likes=33, replies=40, reposts=2, reply_ages=(1, 2, 6))
WHO_ACTIVITY = Post("Health Facts", "healthfactsdaily",
                    "The WHO recommends at least 150 minutes of moderate aerobic activity per week for adults.",
                    120, views=18000, likes=410, replies=35, reposts=75)
INFERENCE_PRICES = Post("Nina Park", "ninaparkdata",
                        "Good breakdown of where inference prices actually went this year:",
                        45, views=7200, likes=160, replies=19, reposts=24, reply_ages=(3, 8, 14),
                        links=("https://research.example.org/inference-price-index-2026",))
PRICING_PAGES = Post("Tom Reyes", "tomreyesgrowth",
                     "This study of 400 SaaS pricing pages is worth your time.",
                     50, views=6100, likes=140, replies=17, reposts=30, reply_ages=(4, 9, 20),
                     links=("https://research.example.org/saas-pricing-pages-study",))
PROTEIN_TIMING = Post("Sports Science Weekly", "sportsciweekly",
                      "New review on protein timing and muscle growth:",
                      40, views=9800, likes=260, replies=33, reposts=51, reply_ages=(2, 5, 12),
                      links=("https://research.example.org/protein-timing-review",))

RUST_ASYNC = Post("Dev Dan", "devdanrs",
                  "What do you all think about async closures finally being stable in Rust?",
                  50, views=4000, likes=80, replies=30, reposts=5)

# --------------------------------------------------------------------------- the customers

MAYA = Persona(
    key="terse",
    name="Maya (@mayashipsml)",
    about="ML infrastructure engineer. Lowercase, short, dry. Opinions on evals, latency, cost and hype.",
    background="I'm an ML infrastructure engineer. I post about evals, latency, inference cost and running LLMs "
               "in production, with as little hype as possible.",
    samples=(
        "turns out the bottleneck was the retry loop, not the model",
        "every eval set leaks into the prompt by week two. rotate it or stop trusting it",
        "caching the system prompt saved more than switching models. boring wins again",
        "if your agent needs 40 tool calls to rename a file the agent is the bug",
        "p95 is the only latency number users feel",
        "hot take: most rag problems are chunking problems wearing a costume",
        "we cut inference spend by deleting a feature nobody used. no model change",
        "local models are great until you need them to be reliable at 3am",
        "structured outputs killed half my regex. the other half is still there out of spite",
        "benchmarks tell you what a model can do on a good day",
        "the demo took an afternoon. the evals took a month. ship the evals",
        "stop streaming tokens to nobody. check the socket first",
        "nobody reads your 40 page model card. put the one limitation that bites on page one",
        "fine-tuning is a last resort, not a personality",
        "i trust a model that says i don't know more than one with a confidence score",
        "context windows are a budget, not a feature",
        "cheaper per token and more tokens per task is not cheaper",
        "the diff view matters more than the model at this point",
        "writing the prompt took ten minutes. writing the test that proves it works took two days. worth it",
        "if the fix is 'add more context' ask what you're hiding from the retrieval step",
        "most prod incidents in llm apps are timeouts wearing a trench coat",
        "tiny models for routing, big models for the hard 5%. that's the whole architecture",
        "the best observability tool is a table of 50 real inputs you look at every monday",
        "please stop calling a for loop an agent",
    ),
    presses=(
        Press("terse-agree", "star", "Short, lowercase, dry; adds one thing about chunking (overlap, eval leakage, "
              "what recall hides); no invented numbers; not a summary of their post.", RAG_CHUNKS),
        Press("terse-contest", "star", "Disagrees with a reason in her voice (context is a budget; cost and latency); "
              "not sycophantic, not a lecture.", FINETUNE_DEAD),
        Press("terse-question", "star", "Answers the question directly in one line (her p95 or 'look at real inputs' "
              "view); no question back.", ONE_METRIC),
        Press("terse-claim", "star", "The claim is checked on the web and the card links the sources; her take on it "
              "(what it means for small teams) is short and specific.", EU_AI_ACT, expects=("web_check",)),
        Press("terse-link", "star", "Reads the linked price index and adds one dry point from it (cheaper per token "
              "is not cheaper per task); the card cites the page; no figure the page does not give.",
              INFERENCE_PRICES, expects=("link",)),
        Press("terse-offbeat", "star", "Outside her beat: a brief, warm human reply or a sensible wait; never an AI "
              "take on a marathon.", MARATHON),
        Press("terse-create-post", "create_post", "A post on something current in AI infrastructure, specific enough "
              "to have been written this week, in her lowercase dry voice.", expects=("news",)),
        Press("terse-create-reply", "create_reply", "Same bar as terse-agree, written into an empty reply box.",
              RAG_CHUNKS),
        Press("terse-enhance", "enhance", "Keeps her point and her voice (lowercase, short, dry); tighter, not longer; "
              "no new claims.",
              draft_text="i think people overuse agents, most of what they build is just a script with extra steps "
                         "and it costs more and breaks more"),
        Press("terse-chat", "chat", "Two or three concrete, current post ideas in her beat, not generic advice.",
              message="What should I post about today?"),
    ),
)

DANIEL = Persona(
    key="essayist",
    name="Daniel (@danokafor)",
    about="Bootstrapped SaaS founder. Full sentences, capitalised, calm and considered; pricing, churn, customers.",
    background="I run a bootstrapped B2B SaaS company. I write about pricing, churn, support and building a "
               "profitable small business.",
    samples=(
        "The most expensive words in SaaS are \"we'll build it later.\" Later arrives the day your biggest customer "
        "asks for it.",
        "We raised prices last spring. Churn didn't move. What moved was who signed up: fewer tire-kickers, more "
        "teams who actually needed the product.",
        "A lesson from eight years of bootstrapping: your first 100 customers tell you what to build. Your next "
        "1,000 tell you what to stop building.",
        "Annual plans aren't a discount. They're a commitment device, for both sides.",
        "If you can't explain your pricing in one sentence, your customers can't either, and they won't try.",
        "Support tickets are the cheapest product research you will ever get. Read them yourself, every week, even "
        "when you have a team.",
        "Three signs a feature should be killed:\n1. Nobody complains when it breaks.\n2. Sales never mentions it.\n"
        "3. It needs its own onboarding.",
        "Growth that depends on one channel is a dependency, not a strategy.",
        "Your competitor's roadmap is not your problem. Your customer's calendar is.",
        "The day we stopped offering custom contracts to small customers, our sales cycle halved and nobody we "
        "wanted left.",
        "Onboarding is where most churn is decided. By month three it's just paperwork.",
        "Founders underestimate how much trust a clear changelog builds. Customers read it more than your blog.",
        "Hiring your first support person is a product decision. Within a month they will know your product's weak "
        "spots better than you.",
        "I used to think discounts closed deals. They mostly delay them: the buyer waits to see if there's a bigger "
        "one.",
        "Every \"quick integration\" is a permanent maintenance cost. Budget for the second year, not the first week.",
        "The best time to write down why you made a decision is the week you make it. Future you will want the "
        "reasons, not the result.",
        "Freemium is a marketing budget with a product attached. Treat it like one.",
        "A feature request is a problem report in disguise. Ask what they were trying to do when they wanted it.",
        "Profitable and small is a real outcome. It's also the one nobody writes about.",
        "When a customer says your product is \"too expensive,\" they usually mean \"I don't see the value yet.\"",
        "Remote teams don't need more meetings. They need better written decisions.",
        "We lost our biggest customer three years ago. It was the best thing that ever happened to the roadmap.",
        "What I look at every Monday: new trials, trial-to-paid, logo churn, and the three angriest emails.",
        "Most \"enterprise features\" are really features for people who can't say no. SSO is the exception.",
    ),
    presses=(
        Press("essayist-agree", "star", "Full sentences, calm; builds on the price rise with one considered point "
              "(who signs up now, what the lost customers signal); no invented figures.", PRICE_RISE),
        Press("essayist-contest", "star", "A measured disagreement with a reason from his view of freemium (a "
              "marketing budget with a product attached); not combative, not a thread.", FREEMIUM_DEAD),
        Press("essayist-question", "star", "Answers the question from his stated view (support as a product decision) "
              "without inventing a personal history he has not written about.", FIRST_HIRE),
        Press("essayist-claim", "star", "The claim is checked on the web with sources on the card; his point is about "
              "what it means for a small SaaS selling abroad.", STRIPE_CURRENCIES, expects=("web_check",)),
        Press("essayist-link", "star", "Reads the linked pricing-page study and adds one considered point from it; "
              "the card cites the page; only the study's own figures.", PRICING_PAGES, expects=("link",)),
        Press("essayist-lowvalue", "star", "Nothing to add to 'gm': a wait with a short reason.", GM),
        Press("essayist-create-post", "create_post", "A current, specific post in his beat (pricing, churn, "
              "bootstrapping) with his full-sentence, considered voice; could be a short list.", expects=("news",)),
        Press("essayist-create-reply", "create_reply", "Same bar as essayist-agree, written into an empty reply box.",
              PRICE_RISE),
        Press("essayist-enhance", "enhance", "Keeps his point; capitalised, full sentences, one clear line of "
              "reasoning; no new claims.",
              draft_text="we stopped doing custom contracts for small customers and it made sales faster and nobody "
                         "we cared about left, i think more founders should say no to custom deals"),
        Press("essayist-chat", "chat", "Concrete, current post ideas for a bootstrapped SaaS founder, not generic "
              "advice.", message="What should I post about today?"),
    ),
)

ROSA = Persona(
    key="coach",
    name="Rosa (@rosacoaches)",
    about="Strength coach. Energetic, practical, second person, the odd emoji; myths, beginners, consistency.",
    background="I'm a strength coach. I post practical training and nutrition advice for beginners and busy people, "
               "and I bust fitness myths.",
    samples=(
        "You don't need a new program. You need to finish the one you started 💪",
        "Protein at breakfast is the easiest win nobody takes.",
        "If your knees hurt on squats, film them from the side before you blame the squat.",
        "Sleep is the supplement. Everything else is decoration.",
        "Beginners: three full-body days a week beats six days of chaos. Every time.",
        "Stop training to failure on every set. Leave one or two in the tank and come back tomorrow.",
        "Walking counts. It always counted. A daily walk will do more for most people than another HIIT class.",
        "Your warm-up should look like your workout, just lighter.",
        "Creatine is boring, cheap, and works. That's why nobody sells it with a story.",
        "The scale went up after leg day? That's water and glycogen, not fat. Breathe.",
        "Progressive overload doesn't mean adding weight every week. Reps, sets, tempo and rest all count.",
        "Consistency beats intensity. The best workout is the one you'll actually repeat next week.",
        "If you only have 20 minutes: squat, push, pull, carry. Done.",
        "Ab workouts don't burn belly fat. Nothing spot-reduces. Train them for strength, not magic 🙃",
        "Most people don't need a cut. They need three months of eating enough protein and lifting.",
        "Pain that gets worse as you warm up: stop. Pain that fades as you warm up: usually fine, keep an eye on it.",
        "Deadlifts aren't bad for your back. Bad deadlifts are bad for your back.",
        "Track one thing for a month: sleep, steps or protein. Just one. Then add another.",
        "Rest days are when you actually get stronger. Treat them like training.",
        "Your grip gives out before your back? Straps are allowed. Train grip separately.",
        "New year crowd at the gym? Be nice. Everybody started somewhere.",
        "Cardio won't kill your gains. Skipping meals will.",
        "Mobility work is great. Strength through the full range is better.",
        "Hydration tip nobody wants: a glass of water before every coffee ☕",
    ),
    presses=(
        Press("coach-agree", "star", "Energetic and practical; backs the switch to full body with one useful reason "
              "or tip; second person; not a summary.", SPLIT_TO_FULL_BODY),
        Press("coach-contest", "star", "Busts the myth clearly and kindly (meal timing matters far less than totals); "
              "no invented studies or numbers.", EAT_EVERY_3H),
        Press("coach-question", "star", "A direct, practical answer to the protein question with no figures that are "
              "not well established; plain words.", HOW_MUCH_PROTEIN),
        Press("coach-claim", "star", "The WHO claim is checked on the web with sources; her take makes it practical "
              "(walking counts).", WHO_ACTIVITY, expects=("web_check",)),
        Press("coach-link", "star", "Reads the linked review and makes it practical in her voice (totals first, "
              "timing second); the card cites the page; no invented numbers.", PROTEIN_TIMING, expects=("link",)),
        Press("coach-offbeat", "star", "Rust async is outside her beat: a sensible wait, never a forced fitness take.",
              RUST_ASYNC),
        Press("coach-create-post", "create_post", "A current, specific post in fitness or nutrition in her voice, not "
              "an AI or tech topic.", expects=("news",)),
        Press("coach-create-reply", "create_reply", "Same bar as coach-agree, written into an empty reply box.",
              SPLIT_TO_FULL_BODY),
        Press("coach-enhance", "enhance", "Keeps her point; punchy, second person, maybe one emoji; no new claims.",
              draft_text="if you are new to the gym dont copy influencer programs, start with 3 full body days and "
                         "focus on getting stronger slowly"),
        Press("coach-chat", "chat", "Concrete, current post ideas in fitness and nutrition, not tech.",
              message="What should I post about today?"),
    ),
)

NEWCOMER = Persona(
    key="new",
    name="A brand-new account",
    about="Nothing written yet: shows the cold start.",
    presses=(
        Press("new-star", "star", "A sensible, plain reply or a wait, and a card that says drafts are generic until "
              "Starling knows their writing.", RAG_CHUNKS),
        Press("new-create-post", "create_post", "Something sensible, with the same voice note; no pretending to know "
              "the account."),
        Press("new-chat", "chat", "Honest that it knows nothing of them yet, and how to fix that.",
              message="What should I post about today?"),
    ),
)

PERSONAS: tuple[Persona, ...] = (MAYA, DANIEL, ROSA, NEWCOMER)
