"""The offline minutes engine.

No model, no network: this reads the transcript the way a note-taker does,
looking for the handful of sentence shapes that carry meeting outcomes.

The design bet is that meetings are formulaic. People commit to work in a small
number of ways — "I'll do X", "Sam, can you do X", "we need to do X by Friday"
— and they record decisions in an even smaller number of ways. Matching those
shapes catches most of what matters and, unlike a model, never invents an
action nobody agreed to.

Where it is unsure it says so. Anything below LIKELY lands in
:attr:`Minutes.review` instead of the published action register, because
minutes that list work nobody committed to are worse than minutes that miss a
line — the first kind gets someone blamed on Friday.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from datetime import date, timedelta

from ..models import (
    CERTAIN,
    LIKELY,
    POSSIBLE,
    ActionItem,
    Decision,
    Minutes,
    Topic,
    Transcript,
    format_duration,
)

# --------------------------------------------------------------------------
# Vocabulary
# --------------------------------------------------------------------------

STOPWORDS = frozenset(
    ["a", "about", "actually", "after", "again", "all", "also", "am", "an", "and", "any", "are", "as", "at", "back", "be", "because", "been", "before", "being", "between", "both", "but", "by", "can", "cant", "could", "day", "did", "do", "does", "doing", "done", "dont", "down", "each", "even", "ever", "every", "few", "for", "from", "further", "get", "gets", "go", "going", "good", "got", "had", "has", "have", "having", "he", "her", "here", "hers", "him", "his", "how", "i", "if", "in", "into", "is", "it", "its", "itself", "just", "kind", "know", "lets", "like", "little", "ll", "look", "lot", "made", "make", "many", "maybe", "me", "mean", "might", "mine", "more", "most", "much", "must", "my", "need", "needs", "no", "nor", "not", "now", "of", "off", "ok", "okay", "on", "once", "one", "only", "or", "other", "others", "otherwise", "ought", "our", "ours", "out", "over", "own", "put", "really", "right", "said", "same", "say", "says", "see", "she", "should", "so", "some", "something", "sort", "still", "such", "sure", "take", "than", "that", "thats", "the", "their", "theirs", "them", "then", "there", "these", "they", "thing", "things", "think", "this", "those", "though", "thought", "through", "time", "to", "too", "two", "up", "us", "use", "used", "very", "want", "was", "way", "we", "well", "were", "what", "when", "where", "which", "while", "who", "whom", "why", "will", "with", "would", "yeah", "yes", "yet", "you", "your", "yours"]
)

# Words that start a sentence looking exactly like a name and are not one.
# Without this, "Agreed, let's reprioritise" makes Agreed the owner.
NOT_NAMES = frozenset(
    ["agreed", "ok", "okay", "right", "yes", "yeah", "yep", "no", "nope", "sure", "thanks", "thank", "perfect", "great", "good", "morning", "afternoon", "evening", "hi", "hello", "hey", "sorry", "well", "actually", "anyway", "first", "second", "third", "next", "last", "finally", "correct", "exactly", "fine", "true", "false", "maybe", "honestly", "look", "listen", "so", "and", "but", "then", "also", "today", "tomorrow", "tonight", "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday", "january", "february", "march", "april", "may", "june", "july", "august", "september", "october", "november", "december"]
)

# Terms that name a moment or a pleasantry rather than a subject, so they never
# make a good topic title.
_TITLE_STOPWORDS = frozenset(
    ["morning", "afternoon", "evening", "everyone", "everybody", "thanks", "think", "going", "really", "thing", "stuff", "actually", "today", "tomorrow", "yesterday", "sorry", "great", "perfect", "course", "guess", "sort", "kind"]
)

_FILLER_PREFIX = re.compile(
    r"^(?:(?:so|ok|okay|right|alright|well|now|yeah|yep|um|uh|erm|look|listen|and|but|then|"
    r"basically|actually|honestly|i mean|you know|i think|i guess|i suppose|let me see)[,\s]+)+",
    re.IGNORECASE,
)
_TRAILING_NOISE = re.compile(
    r"[\s,]*(?:please|thanks|thank you|if that's ok|if that works|ok|okay|right|yeah)[\s.!?]*$",
    re.IGNORECASE,
)
# Sentence boundaries, plus dashes: people chain two commitments with a dash
# far more often than they chain two clauses that mean one thing.
# The character class holds an em dash and an en dash; transcripts contain both.
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9\"'(])|\s+[\u2014\u2013]\s+|\s+--\s+")

# --------------------------------------------------------------------------
# Action patterns
# --------------------------------------------------------------------------

_EXPLICIT = re.compile(
    r"^\s*(?:action(?:\s+item)?|todo|to-do|task|ai)\s*[:\-]\s*(?P<body>.+)$", re.IGNORECASE
)
_FIRST_PERSON = re.compile(
    r"\bi\s*(?:'ll|will|am going to|'m going to|can|shall)\s+(?P<body>\S.*)$", re.IGNORECASE
)
_FIRST_PERSON_SOFT = re.compile(
    r"\bi\s*(?:'ve got to|need to|should|have to|ought to|want to|will need to)\s+(?P<body>\S.*)$",
    re.IGNORECASE,
)
_ASSIGNED = re.compile(
    r"^(?P<owner>[A-Z][\w'\-]{1,20}(?:\s+[A-Z][\w'\-]{1,20})?)\s+"
    r"(?:will|'ll|is going to|are going to|can|should|needs to|need to|has to|have to|is to|to)\s+"
    r"(?P<body>\S.*)$"
)
_VOCATIVE = re.compile(
    r"^(?P<owner>[A-Z][\w'\-]{1,20}(?:\s+[A-Z][\w'\-]{1,20})?)\s*[,\-—:]\s*(?P<body>\S.*)$"
)
_REQUEST = re.compile(
    r"^(?:can|could|would|will)\s+(?P<owner>you|someone|somebody|anyone)\s+(?P<body>\S.*?)\??$",
    re.IGNORECASE,
)
_PLEASE = re.compile(r"^please\s+(?P<body>\S.*)$", re.IGNORECASE)
_COLLECTIVE = re.compile(
    r"^(?:we(?:'ll|'ve|'re|'d)?|let'?s)\s+"
    r"(?:will|need to|needs to|should|have to|has to|must|are going to|'re going to|going to|"
    r"ought to|want to|could|can)?\s*(?P<body>\S.*)$",
    re.IGNORECASE,
)
_SOMEONE = re.compile(
    r"^(?:someone|somebody)\s+(?:needs to|should|has to|must|will)\s+(?P<body>\S.*)$", re.IGNORECASE
)

# Things that look like commitments but are not.
_PAST_TENSE = re.compile(
    r"\b(?:i|we|they|he|she)(?:'ve|'d)?\s+(?:already\s+)?(?:did|sent|shipped|finished|completed|"
    r"closed|fixed|had|made|talked|spoke|met|reviewed|checked|tried|raised|built|wrote|ran)\b",
    re.IGNORECASE,
)
# A past-time expression anywhere in the sentence: it is reporting, not planning.
_PAST_MARKER = re.compile(
    r"\b(?:ago|yesterday|last (?:week|month|quarter|year|time|night|sprint)|previously|back then|"
    r"a fortnight ago|earlier (?:today|this week))\b",
    re.IGNORECASE,
)
_HYPOTHETICAL = re.compile(
    r"^(?:if|unless|suppose|imagine|what if|assuming|in case)\b|"
    r"\b(?:would have|might have|could have|if we (?:did|had|were)|hypothetically)\b",
    re.IGNORECASE,
)
# A task must start with something that can be done. "This is yours" cannot.
_WEAK_TASK = re.compile(
    r"^(?:be|been|is|are|was|were|am|think|thought|feel|felt|believe|agree|disagree|see|hear|"
    r"know|understand|guess|hope|say|said|like|love|hate|remember|mean|"
    r"this|that|it|there|these|those|he|she|they|we|you|i|my|your|his|her|"
    r"don't|dont|do not|can't|cant|won't|wont|didn't|didnt|aren't|isn't|not)\b",
    re.IGNORECASE,
)
_SOCIAL = re.compile(
    r"^(?:can you hear|can everyone hear|can you see|does that make sense|are we all|is everyone|"
    r"shall we start|let's start|let's begin|let's get started|let's move on|welcome|good morning|"
    r"good afternoon|thanks everyone|thank you everyone|anything else)\b",
    re.IGNORECASE,
)

# --------------------------------------------------------------------------
# Decision patterns
# --------------------------------------------------------------------------

_DECISION = re.compile(
    r"\b(?:we(?:'ve| have)? (?:decided|agreed|settled on|landed on|concluded)|"
    r"(?:it|that)(?:'s| is) (?:decided|agreed|settled|approved|signed off)|"
    r"the decision is|decision:|we(?:'re| are) going (?:to go )?with|"
    r"let'?s go with|we'?ll go with|we(?:'ve| have) chosen|we choose|"
    r"sign(?:ed)? off on|green ?light|that's decided|that is decided|"
    r"so we're (?:not )?(?:doing|going)|final answer|that's the call)\b",
    re.IGNORECASE,
)
_DECISION_NEGATIVE = re.compile(
    r"\b(?:have we|did we|should we|do we|are we|shall we|has it been|was it)\b", re.IGNORECASE
)

# --------------------------------------------------------------------------
# Open questions
# --------------------------------------------------------------------------

_OPEN_MARKER = re.compile(
    r"\b(?:open question|still (?:unclear|unknown|not sure|don't know|dont know)|"
    r"we (?:don't|dont|do not) know|to be (?:confirmed|decided)|tbc|tbd|"
    r"need to (?:find out|check|confirm|clarify)|unresolved|"
    r"park(?:ed)? (?:that|this|it)|come back to (?:this|that))\b",
    re.IGNORECASE,
)
_RHETORICAL = re.compile(
    r"^(?:right|ok|okay|yeah|sorry|what|huh|really|sure|any questions|questions|make sense|"
    r"does that (?:work|make sense)|can you hear me|is that (?:ok|okay|right)|you know|"
    r"anything else|how long|no)\b",
    re.IGNORECASE,
)

# --------------------------------------------------------------------------
# Agenda / topic boundaries
# --------------------------------------------------------------------------

_TOPIC_MARKER = re.compile(
    r"^(?:(?:ok|okay|right|alright|so|and)[,\s]+)*"
    r"(?:(?:let's|lets|let us|shall we|can we|time to|now)\s+"
    r"(?:move on to|move onto|turn to|talk about|discuss|cover|look at|get into|switch to|"
    r"jump (?:in)?to|start (?:on|with)|pick up)|"
    r"(?:moving on to|moving on|next up|next item|next topic|next(?:,| )|"
    r"first (?:item|up|thing)|second item|third item|last item|final item|"
    r"item (?:one|two|three|four|1|2|3|4)|on to|onto)|"
    r"(?:the (?:first|second|third|next|last|final) (?:item|topic|thing) is))"
    r"\s*[:\-,]?\s*(?P<label>.*)$",
    re.IGNORECASE,
)

_WEEKDAYS = {
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
    "saturday": 5,
    "sunday": 6,
}
_DUE = re.compile(
    r"\b(?:by|before|due|on|until|no later than)\s+"
    r"(?P<phrase>(?:the\s+)?(?:end of (?:the )?(?:day|week|month|quarter|sprint)|eod|eow|cob|"
    r"close of (?:business|play)|today|tomorrow|tonight|"
    r"(?:next|this|coming|following)\s+(?:week|month|monday|tuesday|wednesday|thursday|friday|"
    r"saturday|sunday)|"
    r"monday|tuesday|wednesday|thursday|friday|saturday|sunday|"
    r"\d{1,2}(?:st|nd|rd|th)?\s+(?:of\s+)?(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*|"
    r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\s+\d{1,2}(?:st|nd|rd|th)?|"
    r"\d{4}-\d{2}-\d{2}))\b",
    re.IGNORECASE,
)
# "I'll email them today" — a deadline with no preposition in front of it.
_BARE_DUE = re.compile(r"\b(?P<phrase>today|tomorrow|tonight|this afternoon)\b", re.IGNORECASE)
_IN_N = re.compile(
    r"\bin\s+(?P<count>a|one|two|three|four|five|six|\d{1,2})\s+(?P<unit>day|days|week|weeks)\b",
    re.IGNORECASE,
)
# A sentence that is nothing but a deadline, said right after the commitment:
# "Give me until Thursday." / "By Friday." / "End of week."
_ORPHAN_DUE = re.compile(
    r"^(?:(?:give|gimme)\s+(?:me|us)\s+(?:until|till|til)\s+|by\s+|before\s+|due\s+|"
    r"that'?s\s+|it\s+should\s+(?:ship|land|be done)\s+by\s+|target(?:ing)?\s+|aim(?:ing)?\s+for\s+)?"
    r"(?P<rest>.+)$",
    re.IGNORECASE,
)
_NUMBER_WORDS = {"a": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6}
_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}


@dataclass
class Unit:
    """One sentence, with the context needed to cite and attribute it."""

    text: str
    speaker: str
    segment_index: int
    position: int


class RulesEngine:
    """Pattern-driven minutes. Deterministic, offline, no dependencies."""

    name = "rules"

    def __init__(
        self,
        non_owners: tuple[str, ...] = ("team", "we", "everyone", "all", "group"),
        max_topics: int = 6,
    ) -> None:
        self.non_owners = frozenset(n.lower() for n in non_owners)
        self.max_topics = max(1, max_topics)

    # -- public API -------------------------------------------------------

    def summarise(
        self,
        transcript: Transcript,
        *,
        title: str = "",
        held_on: date | None = None,
        meeting_id: str = "",
    ) -> Minutes:
        reference = held_on or date.today()
        units = _split_units(transcript)
        known = _known_names(transcript, units)

        actions, review, claimed = self._actions(units, known, reference)
        decisions = self._decisions(units)
        topics = self._topics(units)
        questions = self._questions(units, claimed)
        attendees = transcript.speakers()

        return Minutes(
            meeting_id=meeting_id,
            title=title or _derive_title(topics),
            held_on=reference.isoformat(),
            attendees=attendees,
            summary=_summary(transcript, attendees, topics, decisions, actions),
            topics=topics,
            decisions=decisions,
            actions=actions,
            review=review,
            open_questions=questions,
            engine=self.name,
        )

    # -- actions ----------------------------------------------------------

    def _actions(
        self, units: list[Unit], known: set[str], reference: date
    ) -> tuple[list[ActionItem], list[ActionItem], set[int]]:
        found: list[tuple[Unit, ActionItem]] = []
        for unit in units:
            item = self._action_from(unit, known, reference)
            if item is not None:
                found.append((unit, item))

        _attach_orphan_deadlines(units, found, reference)

        deduped: dict[str, ActionItem] = {}
        for _unit, item in found:
            key = item.key()
            existing = deduped.get(key)
            # The same commitment said twice: keep the more confident record,
            # and let a later mention supply a due date the first one lacked.
            if existing is None:
                deduped[key] = item
            elif item.confidence > existing.confidence:
                item.due = item.due or existing.due
                deduped[key] = item
            elif not existing.due and item.due:
                existing.due = item.due

        ordered = sorted(deduped.values(), key=lambda a: (a.segment_index or 0))
        published = [a for a in ordered if a.confidence >= LIKELY]
        review = [a for a in ordered if a.confidence < LIKELY]
        claimed = {unit.position for unit, _item in found}
        return published, review, claimed

    def _action_from(self, unit: Unit, known: set[str], reference: date) -> ActionItem | None:
        raw = unit.text.strip()
        if len(raw.split()) < 3 or _SOCIAL.match(raw):
            return None

        cleaned = _FILLER_PREFIX.sub("", raw).strip()
        if not cleaned:
            return None

        owner = ""
        confidence = 0
        body = ""

        explicit = _EXPLICIT.match(cleaned)
        if explicit:
            body, confidence = explicit.group("body"), CERTAIN
            owner = _leading_name(body, known) or unit.speaker
            body = _strip_leading_name(body, owner)
        elif _HYPOTHETICAL.search(cleaned):
            return None
        else:
            if (fp := _FIRST_PERSON.search(cleaned)) is not None:
                body, owner, confidence = fp.group("body"), unit.speaker, CERTAIN
            elif (soft := _FIRST_PERSON_SOFT.search(cleaned)) is not None:
                body, owner, confidence = soft.group("body"), unit.speaker, LIKELY
            elif (voc := _VOCATIVE.match(cleaned)) and self._is_owner(voc.group("owner"), known):
                body, owner, confidence = voc.group("body"), voc.group("owner"), CERTAIN
                stripped = _FILLER_PREFIX.sub("", body).strip()
                inner = _REQUEST.match(stripped) or _PLEASE.match(stripped)
                if inner:
                    body = inner.group("body")
            elif (asg := _ASSIGNED.match(cleaned)) and self._is_owner(asg.group("owner"), known):
                body, owner, confidence = asg.group("body"), asg.group("owner"), CERTAIN
            elif (req := _REQUEST.match(cleaned)) is not None:
                body = req.group("body")
                trailing = _trailing_name(cleaned, known)
                owner = trailing or ""
                confidence = CERTAIN if trailing else LIKELY
            elif (pls := _PLEASE.match(cleaned)) is not None:
                body, confidence = pls.group("body"), LIKELY
                owner = _trailing_name(cleaned, known) or ""
            elif (smb := _SOMEONE.match(cleaned)) is not None:
                body, confidence = smb.group("body"), POSSIBLE
            elif (col := _COLLECTIVE.match(cleaned)) is not None:
                # A sentence that records a decision is minuted as one; it
                # should not also show up as a vague half-action.
                if _DECISION.search(cleaned):
                    return None
                body, confidence = col.group("body"), POSSIBLE
                # "we need to" is a weaker signal than "we'll", but a stated
                # deadline means somebody has a date in mind — promote it.
                if _DUE.search(cleaned) or _IN_N.search(cleaned):
                    confidence = LIKELY
            else:
                return None

        if not body or body.lstrip().startswith("'"):
            return None
        if confidence < CERTAIN and (_PAST_TENSE.search(cleaned) or _PAST_MARKER.search(cleaned)):
            return None

        due = _due_date(cleaned, reference)
        task = _to_task(body)
        if not task or len(task.split()) < 2 or _WEAK_TASK.match(task):
            return None
        # "Take that too" is a real commitment and a useless minute: every word
        # in it points at something said earlier. Keep it, but for review only.
        if not _has_content(task):
            confidence = min(confidence, POSSIBLE)

        if owner.lower() in self.non_owners:
            owner = ""
        return ActionItem(
            text=task,
            owner=owner,
            due=due,
            confidence=confidence,
            segment_index=unit.segment_index,
            quote=raw,
        )

    def _is_owner(self, candidate: str, known: set[str]) -> bool:
        lowered = candidate.lower()
        if lowered in self.non_owners or lowered in NOT_NAMES or lowered in STOPWORDS:
            return False
        if lowered in known:
            return True
        # An unfamiliar name is still a name if it is addressed like one, but
        # only single capitalised words qualify — "Next Thursday, can you"
        # should not make Thursday an owner.
        return bool(re.fullmatch(r"[A-Z][a-z]{2,}", candidate))

    # -- decisions --------------------------------------------------------

    def _decisions(self, units: list[Unit]) -> list[Decision]:
        out: list[Decision] = []
        seen: set[str] = set()
        for unit in units:
            text = unit.text.strip()
            if not _DECISION.search(text) or _DECISION_NEGATIVE.search(text):
                continue
            if text.rstrip().endswith("?"):
                continue
            statement = _tidy(_FILLER_PREFIX.sub("", text))
            key = re.sub(r"[^a-z0-9 ]+", " ", statement.lower()).strip()
            if not key or key in seen or len(statement.split()) < 3:
                continue
            seen.add(key)
            out.append(Decision(text=statement, segment_index=unit.segment_index, quote=text))
        return out

    # -- open questions ---------------------------------------------------

    def _questions(self, units: list[Unit], claimed: set[int]) -> list[str]:
        out: list[str] = []
        for unit in units:
            # A question that was already minuted as an action ("Sam, can you
            # write that up?") is answered by the action, not still open.
            if unit.position in claimed:
                continue
            text = unit.text.strip()
            body = _FILLER_PREFIX.sub("", text).strip()
            if not body or _RHETORICAL.match(body) or _SOCIAL.match(body):
                continue
            marked = bool(_OPEN_MARKER.search(body))
            asked = body.endswith("?") and len(body.split()) >= 6
            if not (marked or asked):
                continue
            statement = _tidy(body)
            # "We don't know how many that is" says nothing without the
            # sentence before it, and the minutes do not carry that sentence.
            if not _has_content(statement):
                continue
            # People restate the same uncertainty two or three ways in a row;
            # keep the first phrasing only.
            if any(_similar(statement, other) for other in out):
                continue
            out.append(statement)
        return out[:8]

    # -- topics -----------------------------------------------------------

    def _topics(self, units: list[Unit]) -> list[Topic]:
        if not units:
            return []
        boundaries = _agenda_boundaries(units)
        if len(boundaries) < 2:
            boundaries = _even_boundaries(len(units), self.max_topics)

        corpus = Counter(_keywords(" ".join(u.text for u in units)))
        topics: list[Topic] = []
        for position, (start, end, label) in enumerate(boundaries):
            chunk = units[start:end]
            if not chunk:
                continue
            title = label or _chunk_title(chunk, corpus, first=position == 0)
            points = _chunk_points(chunk)
            if not title and not points:
                continue
            topics.append(
                Topic(
                    title=title or "Discussion",
                    points=points,
                    segment_index=chunk[0].segment_index,
                )
            )
        return topics[: self.max_topics]


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def _split_units(transcript: Transcript) -> list[Unit]:
    units: list[Unit] = []
    for seg in transcript.segments:
        for sentence in _SENTENCE_SPLIT.split(seg.text.strip()):
            sentence = sentence.strip()
            if sentence:
                units.append(
                    Unit(
                        text=sentence,
                        speaker=seg.speaker,
                        segment_index=seg.index,
                        position=len(units),
                    )
                )
    return units


def _known_names(transcript: Transcript, units: list[Unit]) -> set[str]:
    """Names we are willing to treat as owners.

    Speakers are certain. Beyond that, a capitalised word used in the vocative
    ("Sam, can you...") is almost always a person in the room whose microphone
    we never heard — unless it is one of the interjections that happen to sit
    in the same position ("Agreed, let's ship it").
    """
    names = {s.lower() for s in transcript.speakers()}
    for speaker in transcript.speakers():
        parts = speaker.split()
        if parts:
            names.add(parts[0].lower())
    for unit in units:
        match = re.match(r"^([A-Z][a-z]{2,})\s*,", unit.text.strip())
        if match:
            names.add(match.group(1).lower())
    return names - STOPWORDS - NOT_NAMES


def _leading_name(text: str, known: set[str]) -> str:
    match = re.match(r"^\s*([A-Z][\w'\-]{1,20})\b", text)
    if match and match.group(1).lower() in known:
        return match.group(1)
    return ""


def _strip_leading_name(text: str, owner: str) -> str:
    if not owner:
        return text
    pattern = rf"^\s*{re.escape(owner)}\s*(?:,|to|will|should|-|—|:)?\s*"
    return re.sub(pattern, "", text, count=1, flags=re.IGNORECASE)


def _trailing_name(text: str, known: set[str]) -> str:
    """Catch "can you send that over, Priya?"."""
    match = re.search(r",\s*([A-Z][\w'\-]{1,20})\s*\??$", text.strip())
    if match and match.group(1).lower() in known:
        return match.group(1)
    return ""


def _to_task(body: str) -> str:
    """Rewrite a spoken clause as an imperative task."""
    text = _FILLER_PREFIX.sub("", body).strip()
    text = re.sub(
        r"^(?:i|we|you|they|he|she)\s*"
        r"(?:'ll|will|am going to|'m going to|are going to|'re going to|is going to|going to|"
        r"need to|needs to|should|shall|must|have to|has to|ought to|can|could|want to)\s+",
        "",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(r"^(?:to|just|then|also|maybe|probably|quickly)\s+", "", text, flags=re.IGNORECASE)
    # The deadline lives in its own field; leaving it in the text duplicates it.
    text = _DUE.sub("", text)
    text = _IN_N.sub("", text)
    text = _BARE_DUE.sub("", text)
    text = _TRAILING_NOISE.sub("", text)
    # Removing "by Friday" from the middle of a clause leaves double spaces
    # and orphaned punctuation behind.
    text = re.sub(r"\s+([.,;!?])", r"\1", text)
    text = re.sub(r"\s{2,}", " ", text).strip(" ,;-—")
    return _tidy(text).rstrip(" .")


def _tidy(text: str) -> str:
    text = re.sub(r"\s{2,}", " ", text.strip())
    text = text.strip(" ,;")
    if not text:
        return ""
    return text[0].upper() + text[1:]


def _attach_orphan_deadlines(
    units: list[Unit], found: list[tuple[Unit, ActionItem]], reference: date
) -> None:
    """Give "Give me until Thursday" to the commitment it follows.

    People routinely split a commitment from its deadline across two sentences.
    A short sentence that resolves to a date, spoken by the same person within
    a couple of sentences of their own commitment, belongs to that commitment.
    """
    by_position = {unit.position: item for unit, item in found}
    for unit in units:
        if unit.position in by_position or len(unit.text.split()) > 8:
            continue
        stripped = _ORPHAN_DUE.match(unit.text.strip())
        if stripped is None:
            continue
        due = _due_date(unit.text, reference) or _due_date(f"by {stripped.group('rest')}", reference)
        if not due:
            continue
        for offset in (1, 2, 3):
            candidate = by_position.get(unit.position - offset)
            if candidate is None:
                continue
            owner_speaker = units[unit.position - offset].speaker
            if owner_speaker == unit.speaker and not candidate.due:
                candidate.due = due
            break


def _due_date(text: str, reference: date) -> str:
    """Resolve a spoken deadline to an ISO date, or keep the phrase."""
    relative = _IN_N.search(text)
    if relative:
        raw = relative.group("count").lower()
        count = _NUMBER_WORDS.get(raw, 0) or (int(raw) if raw.isdigit() else 0)
        if count:
            days = count * (7 if relative.group("unit").lower().startswith("week") else 1)
            return (reference + timedelta(days=days)).isoformat()

    match = _DUE.search(text) or _BARE_DUE.search(text)
    if not match:
        return ""
    phrase = re.sub(r"^the\s+", "", match.group("phrase").strip().lower())

    if phrase in {
        "today", "eod", "cob", "close of business", "close of play",
        "end of day", "end of the day", "tonight", "this afternoon",
    }:
        return reference.isoformat()
    if phrase == "tomorrow":
        return (reference + timedelta(days=1)).isoformat()
    if phrase in {"eow", "end of week", "end of the week"}:
        return (reference + timedelta(days=(4 - reference.weekday()) % 7)).isoformat()
    if phrase in {"end of month", "end of the month"}:
        return _end_of_month(reference).isoformat()
    if phrase in {"end of quarter", "end of the quarter", "end of sprint", "end of the sprint"}:
        return phrase
    if phrase in {"next week", "this week", "coming week", "following week"}:
        offset = 7 if phrase.startswith(("next", "following", "coming")) else 0
        anchor = reference + timedelta(days=offset)
        return (anchor + timedelta(days=(4 - anchor.weekday()) % 7)).isoformat()
    if phrase in {"next month", "this month"}:
        return phrase
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", phrase):
        return phrase

    calendar = _calendar_date(phrase, reference)
    if calendar:
        return calendar

    weekday_match = re.fullmatch(
        r"(?:(next|this|coming|following)\s+)?"
        r"(monday|tuesday|wednesday|thursday|friday|saturday|sunday)",
        phrase,
    )
    if weekday_match:
        target = _WEEKDAYS[weekday_match.group(2)]
        ahead = (target - reference.weekday()) % 7 or 7
        if weekday_match.group(1) in {"next", "following"} and ahead <= 6:
            ahead += 7
        return (reference + timedelta(days=ahead)).isoformat()

    return phrase


def _calendar_date(phrase: str, reference: date) -> str:
    """"5th of March" / "March 5" to an ISO date in the nearest sensible year."""
    day = month = 0
    first = re.fullmatch(r"(\d{1,2})(?:st|nd|rd|th)?\s+(?:of\s+)?([a-z]{3,9})", phrase)
    second = re.fullmatch(r"([a-z]{3,9})\s+(\d{1,2})(?:st|nd|rd|th)?", phrase)
    if first:
        day, month = int(first.group(1)), _MONTHS.get(first.group(2)[:3], 0)
    elif second:
        month, day = _MONTHS.get(second.group(1)[:3], 0), int(second.group(2))
    if not (month and 1 <= day <= 31):
        return ""
    try:
        candidate = date(reference.year, month, day)
    except ValueError:
        return ""
    # A date already past is next year's — people schedule forwards.
    if candidate < reference:
        try:
            candidate = date(reference.year + 1, month, day)
        except ValueError:
            return ""
    return candidate.isoformat()


def _end_of_month(reference: date) -> date:
    if reference.month == 12:
        return date(reference.year, 12, 31)
    return date(reference.year, reference.month + 1, 1) - timedelta(days=1)


def _has_content(text: str) -> bool:
    """Whether a phrase says anything on its own, or is all pronouns."""
    # Apostrophes are dropped first so that "don't" is tested as one word
    # rather than as "don" plus a stray "t".
    words = re.findall(r"[a-z]+", text.lower().replace("'", "").replace("\u2019", ""))
    return any(word not in STOPWORDS and len(word) >= 3 for word in words)


def _keywords(text: str) -> list[str]:
    words = re.findall(r"[A-Za-z][A-Za-z'\-]{2,}", text.lower())
    return [w for w in words if w not in STOPWORDS and len(w) > 3]


def _agenda_boundaries(units: list[Unit]) -> list[tuple[int, int, str]]:
    """Cut the meeting where someone announced a new item."""
    cuts: list[tuple[int, str]] = []
    for i, unit in enumerate(units):
        match = _TOPIC_MARKER.match(unit.text.strip())
        if not match:
            continue
        label = _clean_label(match.group("label"))
        # Ignore a marker that fires on top of another one.
        if cuts and i - cuts[-1][0] < 3:
            continue
        cuts.append((i, label))

    if not cuts:
        return []
    if cuts[0][0] > 2:
        cuts.insert(0, (0, ""))
    spans: list[tuple[int, int, str]] = []
    for idx, (start, label) in enumerate(cuts):
        end = cuts[idx + 1][0] if idx + 1 < len(cuts) else len(units)
        spans.append((start, end, label))
    return spans


def _clean_label(raw: str) -> str:
    """Turn "the pilot rollout. Sam, this is yours" into "Pilot rollout"."""
    label = re.split(r"[.?!;]", raw.strip(), maxsplit=1)[0]
    label = label.strip().strip(",:- ")
    label = re.sub(r"^(?:the|our|this|that|a)\s+", "", label, flags=re.IGNORECASE)
    label = re.sub(r"\s+(?:now|then|next|please)$", "", label, flags=re.IGNORECASE)
    if len(label.split()) > 8:
        label = " ".join(label.split()[:8])
    return _tidy(label)


def _even_boundaries(count: int, max_topics: int) -> list[tuple[int, int, str]]:
    """Fall back to equal slices when nobody announced an agenda."""
    if count == 0:
        return []
    chunks = max(1, min(max_topics, count // 12 or 1))
    size = max(1, count // chunks)
    spans: list[tuple[int, int, str]] = []
    start = 0
    while start < count:
        end = min(count, start + size)
        # Absorb a stub tail rather than emitting a two-sentence "topic".
        if count - end < size // 2:
            end = count
        spans.append((start, end, ""))
        start = end
    return spans


def _chunk_title(chunk: list[Unit], corpus: Counter[str], first: bool = False) -> str:
    """Name a chunk by the terms it uses more than the meeting as a whole."""
    local = Counter(
        word for word in _keywords(" ".join(u.text for u in chunk))
        if word not in _TITLE_STOPWORDS
    )
    repeated = {word: count for word, count in local.items() if count > 1}
    if not repeated:
        # Nothing in this stretch is talked about twice, so there is no subject
        # to name. Greetings and scene-setting land here.
        return "Opening" if first else ""
    total = sum(corpus.values()) or 1
    scored = sorted(
        repeated.items(),
        key=lambda kv: (kv[1] / (corpus.get(kv[0], 1) / total)) if corpus.get(kv[0]) else kv[1],
        reverse=True,
    )
    return ", ".join(word.capitalize() for word, _count in scored[:3])


def _chunk_points(chunk: list[Unit], limit: int = 3) -> list[str]:
    """Pick the sentences that carry the most content words."""
    scored: list[tuple[float, int, str]] = []
    for unit in chunk:
        text = _tidy(_FILLER_PREFIX.sub("", unit.text))
        words = text.split()
        if len(words) < 6 or text.endswith("?"):
            continue
        density = len(_keywords(text)) / len(words)
        length_bonus = min(len(words), 30) / 30
        scored.append((density + length_bonus, unit.position, text))
    scored.sort(key=lambda row: row[0], reverse=True)

    chosen: list[tuple[int, str]] = []
    for _score, position, text in scored:
        if any(_similar(text, other) for _pos, other in chosen):
            continue
        chosen.append((position, text))
        if len(chosen) == limit:
            break
    return [text for _pos, text in sorted(chosen)]


def _similar(left: str, right: str) -> bool:
    a, b = set(_keywords(left)), set(_keywords(right))
    if not a or not b:
        return False
    return len(a & b) / len(a | b) > 0.5


def _derive_title(topics: list[Topic]) -> str:
    for topic in topics:
        if topic.title and topic.title not in {"Discussion", "Opening"}:
            return f"Meeting — {topic.title}"
    return "Meeting"


def _summary(
    transcript: Transcript,
    attendees: list[str],
    topics: list[Topic],
    decisions: list[Decision],
    actions: list[ActionItem],
) -> str:
    """Two or three sentences of orientation, all of it verifiable."""
    parts: list[str] = []
    who = (
        f"{len(attendees)} participants ({', '.join(attendees[:6])}{'…' if len(attendees) > 6 else ''})"
        if attendees
        else "The meeting"
    )
    length = format_duration(transcript.duration)
    opener = f"{who} met" if attendees else "The meeting ran"
    parts.append(f"{opener} for {length}." if length != "unknown" else f"{opener}.")

    titles = [t.title for t in topics if t.title not in {"", "Discussion", "Opening"}]
    if titles:
        listed = ", ".join(titles[:-1]) + f" and {titles[-1]}" if len(titles) > 1 else titles[0]
        parts.append(f"Discussion covered {listed}.")

    counts: list[str] = []
    if decisions:
        counts.append(f"{len(decisions)} decision{'s' if len(decisions) != 1 else ''} recorded")
    if actions:
        owned = sum(1 for a in actions if a.owner)
        detail = f" ({owned} with a named owner)" if owned else ""
        counts.append(f"{len(actions)} action point{'s' if len(actions) != 1 else ''}{detail}")
    parts.append(
        _tidy(" and ".join(counts)) + "."
        if counts
        else "No decisions or action points were detected."
    )
    return " ".join(parts)
