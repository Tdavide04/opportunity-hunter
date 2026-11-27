"""Relevance: is this Listing the kind of object the user wants for this Search Profile? Pure: no I/O.

Learned per profile from the user's decisions on its Opportunities with a naive Bayes over title words:
for each word, how much more often it appears in the titles the user kept than in those they dismissed
as the wrong object. Only words seen in at least MIN_WORD_COUNT decided titles count, each capped at
MAX_WORD_WEIGHT, so one odd title cannot dominate. The prior is neutral: a title with no known word scores 50.
"""
import math
from collections.abc import Iterable
from dataclasses import dataclass

from opportunity_hunter.hunter.matching import normalize

NEUTRAL = 50
MIN_EXAMPLES_PER_CLASS = 3  # below this the profile has no model and every Listing scores NEUTRAL
MIN_WORD_COUNT = 2
MAX_WORD_WEIGHT = 2.0  # log-odds; e^2 ≈ 7 times more likely
SMOOTHING = 2.0  # m of the m-estimate, in titles
EXPLAINED_WORDS = 3
# articles and prepositions of the Marketplaces' languages: they weigh (they tell the title's language, and the
# language often tells the region) but they are not shown as the reason of a Relevance, and neither are numbers
UNEXPLAINED = frozenset(
    "il lo la i gli le un una uno di da in con su per tra fra del della dei delle al alla e o "
    "the a an of for to and with in on or "
    "le la les un une des de du et pour avec en au aux "
    "el los las unos unas del para con y en por "
    "der die das ein eine und mit fur von zu im".split()
)

# How each decision teaches relevance: (relevant?, weight). A dismissal for price or condition means the
# object was the right one; a dismissal without a reason (older decisions, bulk dismissals) is weak evidence.
KEPT = (True, 1.0)
DISMISSED_FOR = {
    "other_item": (False, 1.0),
    "accessory": (False, 1.0),
    "region": (False, 1.0),
    "too_expensive": (True, 1.0),
    "condition": (True, 1.0),
}
DISMISSED_WITHOUT_REASON = (False, 0.5)


@dataclass(frozen=True)
class Example:
    title: str
    relevant: bool
    weight: float


@dataclass(frozen=True)
class Judgement:
    value: int  # 0-100
    words: list[tuple[str, float]]  # the words that weighed most, with their log-odds (+ for, - against)
    learning: bool  # True while the profile has too few decisions for a model


def label(status: str, dismiss_reason: str | None) -> tuple[bool, float] | None:
    """What an Opportunity's status teaches, or None while the user has not decided (new)."""
    if status in ("seen", "pursued", "acquired"):
        return KEPT
    if status == "dismissed":
        return DISMISSED_FOR.get(dismiss_reason or "", DISMISSED_WITHOUT_REASON)
    return None


def words(title: str | None, keywords: Iterable[str]) -> set[str]:
    """Title words except the profile's own keywords (every Match contains them, they teach nothing)."""
    own = {word for keyword in keywords for word in normalize(keyword).split()}
    return {word for word in normalize(title).split() if word not in own and (len(word) > 1 or word.isdigit())}


class RelevanceModel:
    def __init__(self, weights: dict[str, float]) -> None:
        self.weights = weights

    def judge(self, title: str | None, keywords: Iterable[str]) -> Judgement:
        present = sorted(((w, self.weights[w]) for w in words(title, keywords) if w in self.weights), key=lambda item: -abs(item[1]))
        logit = sum(weight for _, weight in present)
        # numbers weigh too (a sequel, a lot of 2) but read as noise next to a title
        explained = [(w, round(x, 2)) for w, x in present if w not in UNEXPLAINED and not w.isdigit()][:EXPLAINED_WORDS]
        return Judgement(round(100 / (1 + math.exp(-logit))), explained, learning=False)


def train(examples: Iterable[Example], keywords: Iterable[str]) -> RelevanceModel | None:
    keywords = list(keywords)
    totals = {True: 0.0, False: 0.0}
    sizes = {True: 0, False: 0}
    counts: dict[str, dict[bool, float]] = {}
    documents: dict[str, int] = {}
    for example in examples:
        totals[example.relevant] += example.weight
        sizes[example.relevant] += 1
        for word in words(example.title, keywords):
            counts.setdefault(word, {True: 0.0, False: 0.0})[example.relevant] += example.weight
            documents[word] = documents.get(word, 0) + 1
    if min(sizes.values()) < MIN_EXAMPLES_PER_CLASS:
        return None
    weights = {}
    for word, count in counts.items():
        if documents[word] < MIN_WORD_COUNT:
            continue
        # share of titles of each class containing the word, smoothed toward its share over all decided titles
        # (m-estimate): with few kept titles, plain Laplace smoothing would make every unseen word look relevant
        overall = (count[True] + count[False]) / (totals[True] + totals[False])
        kept = (count[True] + SMOOTHING * overall) / (totals[True] + SMOOTHING)
        dismissed = (count[False] + SMOOTHING * overall) / (totals[False] + SMOOTHING)
        weights[word] = max(-MAX_WORD_WEIGHT, min(MAX_WORD_WEIGHT, math.log(kept / dismissed)))
    return RelevanceModel(weights)


def judge(model: RelevanceModel | None, title: str | None, keywords: Iterable[str]) -> Judgement:
    if model is None:
        return Judgement(NEUTRAL, [], learning=True)
    return model.judge(title, keywords)
