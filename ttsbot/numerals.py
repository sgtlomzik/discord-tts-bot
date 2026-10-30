"""Numbers in a Russian message, spelled out the way they are said.

The cloud voices read the digits of a short Russian message in English
("го в 5" came out as "five"): Fish and MiniMax normalize numbers only
for English and Chinese. speak_numbers() spells them out in agreement
with the words around them: the preposition before a number sets its case
("с 5 до 7" -> "с пяти до семи"), the noun after it the gender and case
("2 минуты", "в 3 играх"), and a noun a count cannot take makes it an
ordinal ("2 место" -> "второе место", "в 2026 году").

num2words spells the numbers, pymorphy3 reads the nouns. Its dictionary
takes ~0.1 s and ~20 MB to load, so the bot loads it at startup
(warm_up) instead of on the first message.

Numbers are read in Russian, the chat's language, unless they stand in
English text: a stretch without Cyrillic that has a common English word
("see you at 5") keeps its digits for the voice to read in English.
Names and chat shorthand ("GTA 5", "lvl 50", "1v1") are read in Russian.
"""

from __future__ import annotations

import functools
import re
from decimal import Decimal
from typing import NamedTuple

import pymorphy3
from num2words import num2words

_DIGIT = re.compile(r"[0-9]")
_LATIN = re.compile(r"[A-Za-z]")
_CYRILLIC = re.compile(r"[А-Яа-яЁё]")
_NON_CYRILLIC = re.compile(r"[^А-Яа-яЁё]+")
# A word, not the letters of "i7" or "CS2".
_LATIN_WORD = re.compile(r"(?<![A-Za-z0-9'])[A-Za-z']+(?![A-Za-z0-9'])")
# A clause of a stretch of Latin text: "го в 5. see you" has two.
_CLAUSE_END = re.compile(r"(?<=[.,!?;:…])(?=\s)|(?=\n)")
# These, at least half the words of a clause, make it English text: one
# in a message without Cyrillic ("I'm 25", "at 5"), two in a Russian one
# ("The Witcher 3 топ" and "Heroes of Might and Magic 3" are names). Chat
# words Russians write in Latin too (ok, go, gg, lvl, min, top) are not
# here, nor "a", a letter of every abbreviation.
_ENGLISH_WORDS = frozenset("""
about after again all also am an and any are as at be been before but by can can't could
day days did do does don't dont for from get got had has have he her here him his how hours
i i'm im if in is it it's its just know last let's like me minutes more my next no not
now of on only or our out over people see she should so some than that the their them then
there these they this those time to too up us very was we were what when where which who why
will with would year years yes you your
""".split())
# English text's digits are hidden from the passes as private-use characters.
_HIDE = str.maketrans("0123456789", "".join(chr(0xE030 + d) for d in range(10)))
_SHOW = str.maketrans("".join(chr(0xE030 + d) for d in range(10)), "0123456789")

_N2W_CASE = {"nomn": "n", "gent": "g", "datv": "d", "accs": "a", "ablt": "i", "loct": "p"}
_N2W_GENDER = {"masc": "m", "femn": "f", "neut": "n"}
# pymorphy3's second locative ("в году") is the case a number agrees in;
# its partitive ("году" as "чаю") never follows a number ("два чая").
_CASE_OF_TAG = {"nomn": "nomn", "gent": "gent", "datv": "datv", "accs": "accs",
                "ablt": "ablt", "loct": "loct", "loc2": "loct"}
_OBLIQUE = ("datv", "ablt", "loct")

# The cases a preposition governs; the first is the one a number takes
# when no noun follows it ("с 5" -> "с пяти", "в 5" -> "в пять").
PREPOSITIONS: dict[str, tuple[str, ...]] = {
    **dict.fromkeys(
        ("без", "для", "до", "из", "из-за", "из-под", "от", "у", "около", "вокруг", "возле",
         "после", "кроме", "против", "среди", "мимо", "вместо", "ради", "вроде", "свыше",
         "более", "менее", "больше", "меньше", "порядка"),
        ("gent",)),
    "с": ("gent", "ablt"), "со": ("gent", "ablt"),
    "к": ("datv",), "ко": ("datv",), "благодаря": ("datv",), "согласно": ("datv",),
    "по": ("accs", "datv"),
    "в": ("accs", "loct"), "во": ("accs", "loct"), "на": ("accs", "loct"),
    "за": ("accs", "ablt"), "под": ("accs", "ablt"),
    "про": ("accs",), "через": ("accs",), "сквозь": ("accs",),
    "над": ("ablt",), "перед": ("ablt",), "между": ("ablt",),
    "о": ("loct",), "об": ("loct",), "обо": ("loct",), "при": ("loct",),
    **dict.fromkeys(
        ("в течение", "в течении", "на протяжении", "в районе", "в размере", "в возрасте",
         "в пределах", "в количестве"),
        ("gent",)),
}
# Cases a bare number agrees in with the noun after it. Without a
# preposition a number is not genitive ("21 числа" is an ordinal) and
# never prepositional.
_BARE_CASES = ("nomn", "accs", "ablt")

# "в 5 утра": the hour of the day, not a count of mornings.
_TIME_OF_DAY = frozenset({"утра", "дня", "вечера", "ночи"})
# Chat abbreviations pymorphy3 takes for other nouns ("мин" for "мина"):
# these count like the word they shorten, the rest say nothing.
_ABBREVIATIONS = {"мин": "минута", "сек": "секунда"}
_NOT_NOUNS = frozenset({"норм", "ммр", "лвл", "хп", "дпс", "кд"})
# Words that govern the genitive of a count after them: "нет 2 рублей".
_GENITIVE_WORDS = frozenset({"нет", "хватает", "хватит", "хватило", "недостаточно", "много", "мало"})
# A singular noun after a number that does not fit a count makes the
# number an ordinal only if it is one of these ("2 место", "21 числа", "с
# 5 этажа"), or an oblique case after a preposition ("в 3 раунде"). Other
# words are the next words of an unpunctuated chat ("дай 5 Вася") or its
# mass nouns ("100 опыта", "500 голды").
_ORDINAL_NOUNS = frozenset({
    "год", "число", "день", "неделя", "месяц", "минута", "секунда", "этаж", "класс", "курс",
    "век", "раунд", "уровень", "урок", "сезон", "тур", "этап", "ранг", "место", "ряд",
    "подъезд", "дом", "номер", "вагон", "пункт", "глава", "серия", "часть", "страница",
    "квартира", "попытка", "матч", "вопрос", "задание", "сет", "тайм", "эпизод", "раз",
    "половина", "смена", "волна", "карта", "палата", "школа",
})
# "1 место" is "первое место": nouns a count of one rarely takes.
_ORDINAL_ONE = frozenset({
    "место", "этаж", "класс", "курс", "раунд", "сезон", "тур", "этап", "уровень", "ранг",
    "ряд", "подъезд", "вагон", "канал", "глава", "серия", "урок", "век", "пункт", "том",
    "сет", "тайм", "эпизод",
})

_MONTHS = ("января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа",
           "сентября", "октября", "ноября", "декабря")
_MONTH = "|".join(_MONTHS)
_DIGIT_NAMES = ("ноль", "один", "два", "три", "четыре", "пять", "шесть", "семь", "восемь", "девять")
_CURRENCIES = {"$": "доллар", "€": "евро", "£": "фунт", "₽": "рубль", "₴": "гривна",
               "руб": "рубль", "р": "рубль"}
_MULTIPLIERS = {"тыс": "тысяча", "млн": "миллион", "млрд": "миллиард", "трлн": "триллион"}
# pymorphy3 has the euro neuter; the dictionaries say "один евро".
_GENDERS = {"евро": "masc"}
# A dotted number after these is a version: "патч 1.12".
_VERSION_WORDS = frozenset({"версия", "версии", "версию", "патч", "патча", "патче", "обнова",
                            "обновление", "апдейт", "билд"})
# A one-digit day and a month without a year are a date only after these
# ("до 1.11"); else "1.12" is a decimal or a version.
_DATE_PREPOSITIONS = frozenset({"с", "со", "до", "по", "к", "ко", "на", "от", "после", "около", "перед"})
# Spelled numbers above this are read digit by digit (num2words stops at 10**33).
_SPELLED_MAX = 10**15
# Feminine adjective endings of "целая"/"десятая": one, and a count of more.
_FEM_ENDINGS = {
    False: {"nomn": "ая", "gent": "ой", "datv": "ой", "accs": "ую", "ablt": "ой", "loct": "ой"},
    True: {"nomn": "ых", "gent": "ых", "datv": "ым", "accs": "ых", "ablt": "ыми", "loct": "ых"},
}
_DENOMINATORS = ("десят", "сот", "тысячн")


class Noun(NamedTuple):
    lemma: str
    plural: bool
    case: str
    gender: str | None
    animate: bool


class Reading(NamedTuple):
    ordinal: bool
    case: str
    gender: str = "masc"
    plural: bool = False
    animate: bool = False


# -- morphology --------------------------------------------------------

@functools.cache
def _analyzer() -> pymorphy3.MorphAnalyzer:
    return pymorphy3.MorphAnalyzer()


def warm_up() -> None:
    """Load the dictionary and spell the small numbers now (~0.2 s), so the
    first messages do not wait for them."""
    _analyzer().parse("минута")
    for n in range(_PRESPELLED + 1):
        for case in _N2W_CASE:
            for animate in (False, True) if case == "accs" else (False,):
                for gender in _N2W_GENDER:
                    _cardinals[n, case, gender, animate] = _spell_cardinal(n, case, gender, animate)
                    _ordinals[n, case, gender, False, animate] = _spell_ordinal(n, case, gender, False, animate)
                _ordinals[n, case, "masc", True, animate] = _spell_ordinal(n, case, "masc", True, animate)


@functools.lru_cache(maxsize=4096)
def _word(word: str) -> tuple[str | None, tuple[Noun, ...]]:
    """The likeliest part of speech of ``word`` and its noun readings
    (none unless it is most likely a noun: "потом" is an adverb)."""
    parses = _analyzer().parse(word)
    if not parses:
        return None, ()
    pos = parses[0].tag.POS
    if pos != "NOUN":
        return pos, ()
    nouns = []
    for parse in parses:
        tag = parse.tag
        case = _CASE_OF_TAG.get(str(tag.case))
        if tag.POS != "NOUN" or case is None or "Fixd" in tag:
            continue  # an indeclinable "фпс" or "кг" says nothing
        gender = str(tag.gender) if tag.gender in _N2W_GENDER else None
        nouns.append(Noun(parse.normal_form, tag.number == "plur", case, gender, tag.animacy == "anim"))
    return pos, tuple(nouns)


@functools.lru_cache(maxsize=1024)
def _counted(lemma: str, n: int | None, case: str) -> str:
    """``lemma`` in the form a count ``n`` in ``case`` takes ("5 процентов",
    "до 50 процентов"); a fraction (``n`` None) takes the genitive singular."""
    parse = _analyzer().parse(lemma)[0]
    if n is None:
        form = parse.inflect({"sing", "gent"})
    elif case == "accs" and _one(n):  # "за 1 гривну"
        form = parse.inflect({"sing", "accs"})
    elif case in ("nomn", "accs"):
        form = parse.make_agree_with_number(n)
    else:
        form = parse.inflect({case, "sing" if _one(n) else "plur"})
    return form.word if form else lemma


@functools.lru_cache(maxsize=256)
def _gender_of(lemma: str) -> str:
    if lemma in _GENDERS:
        return _GENDERS[lemma]
    gender = _analyzer().parse(lemma)[0].tag.gender
    return str(gender) if gender in _N2W_GENDER else "masc"


def _one(n: int) -> bool:
    return n % 10 == 1 and n % 100 != 11


def _few(n: int) -> bool:
    return n % 10 in (2, 3, 4) and n % 100 not in (12, 13, 14)


# -- spelling ----------------------------------------------------------

# Numbers up to this are spelled in every form by warm_up: chat numbers
# are mostly small, and num2words takes ~20 us a call on the server.
_PRESPELLED = 100
_cardinals: dict[tuple[int, str, str, bool], str] = {}
_ordinals: dict[tuple[int, str, str, bool, bool], str] = {}


def cardinal(n: int, case: str = "nomn", gender: str = "masc", animate: bool = False) -> str:
    words = _cardinals.get((n, case, gender, animate))
    return words if words is not None else _spell_cardinal(n, case, gender, animate)


def ordinal(n: int, case: str = "nomn", gender: str = "masc", plural: bool = False,
            animate: bool = False) -> str:
    if plural:
        gender = "masc"  # a plural has no gender
    words = _ordinals.get((n, case, gender, plural, animate))
    return words if words is not None else _spell_ordinal(n, case, gender, plural, animate)


@functools.lru_cache(maxsize=4096)
def _spell_cardinal(n: int, case: str, gender: str, animate: bool) -> str:
    if n >= _SPELLED_MAX:
        return _digits(str(n))
    words = num2words(n, lang="ru", case=_N2W_CASE[case], gender=_N2W_GENDER[gender],
                      animate=animate)
    # "тысяча пятьсот", not "одна тысяча пятьсот".
    head, _, rest = words.partition(" ")
    if head in ("одна", "одной", "одну") and rest.startswith("тысяч"):
        return rest
    return words


@functools.lru_cache(maxsize=4096)
def _spell_ordinal(n: int, case: str, gender: str, plural: bool, animate: bool) -> str:
    if n >= _SPELLED_MAX:
        return _digits(str(n))
    return num2words(n, lang="ru", to="ordinal", case=_N2W_CASE[case],
                     gender=_N2W_GENDER[gender], plural=plural, animate=animate)


def _spell(n: int, reading: Reading) -> str:
    if reading.ordinal:
        return ordinal(n, reading.case, reading.gender, reading.plural, reading.animate)
    return cardinal(n, reading.case, reading.gender, reading.animate)


def _digits(text: str) -> str:
    return " ".join(_DIGIT_NAMES[int(ch)] for ch in text)


def _integer(text: str, case: str = "nomn", gender: str = "masc") -> str:
    """A number as written: "007" and long runs of digits are read digit by digit."""
    if len(text) > 1 and text[0] == "0":
        return _digits(text)
    return cardinal(int(text), case, gender)


def _decimal(whole: str, frac: str, sep: str, case: str = "nomn") -> str:
    if len(frac) > 3:  # "3.14159": the digits
        return f"{_integer(whole, case)} {'точка' if sep == '.' else 'запятая'} {_digits(frac)}"

    def part(n: int, stem: str) -> str:
        return f"{cardinal(n, case, 'femn')} {stem}{_FEM_ENDINGS[not _one(n)][case]}"

    return f"{part(int(whole), 'цел')} {part(int(frac), _DENOMINATORS[len(frac) - 1])}"


def _number(text: str, case: str = "nomn", gender: str = "masc") -> tuple[str, int | None]:
    """An integer or decimal as written, and its value for agreement
    (None for a decimal, which takes the genitive singular)."""
    match = re.fullmatch(r"([0-9]+)(?:([.,])([0-9]+))?", text)
    assert match is not None
    if match.group(3):
        return _decimal(match.group(1), match.group(3), match.group(2), case), None
    return _integer(match.group(1), case, gender), int(match.group(1))


def _sign(sign: str | None) -> str:
    return {"+": "плюс ", "-": "минус ", "−": "минус "}.get(sign or "", "")


# -- context -----------------------------------------------------------

_WORD_AFTER = re.compile(r"[ \xa0]*([А-Яа-яЁё]+)")
_LETTERS = frozenset("абвгдеёжзийклмнопрстуфхцчшщъыьэюяАБВГДЕЁЖЗИЙКЛМНОПРСТУФХЦЧШЩЪЫЬЭЮЯ")


def _words_before(text: str, start: int) -> tuple[str, ...]:
    """The (up to) two Cyrillic words right before ``start``, nearest last,
    lowercased; "из-за" is one word. A scan back, not a regex search: it
    runs for every number."""
    words: list[str] = []
    end = start
    while len(words) < 2:
        while end > 0 and text[end - 1] in " \xa0":
            end -= 1
        begin = end
        while begin > 0 and (text[begin - 1] in _LETTERS
                             or (text[begin - 1] == "-" and begin > 1 and text[begin - 2] in _LETTERS
                                 and begin < end)):
            begin -= 1
        if begin == end:
            break
        words.insert(0, text[begin:end].lower())
        end = begin
    return tuple(words)


def _word_before(text: str, start: int) -> str | None:
    words = _words_before(text, start)
    return words[-1] if words else None


def _preposition(text: str, start: int) -> str | None:
    """The preposition right before ``start``: one word, or two ("в течение")."""
    words = _words_before(text, start)
    if len(words) == 2 and " ".join(words) in PREPOSITIONS:
        return " ".join(words)
    return words[-1] if words and words[-1] in PREPOSITIONS else None


def _cases(prep: str | None) -> tuple[str, ...]:
    return PREPOSITIONS[prep] if prep else _BARE_CASES


def _next_word(text: str, end: int) -> tuple[str | None, int]:
    match = _WORD_AFTER.match(text, end)
    return (match.group(1).lower(), match.end()) if match else (None, end)


@functools.cache
def _abbreviation(word: str) -> tuple[Noun, ...]:
    """Every form of the noun ``word`` shortens: it fits any count."""
    lemma = _ABBREVIATIONS[word]
    return tuple(Noun(lemma, plural, case, _gender_of(lemma), False)
                 for plural in (False, True) for case in _N2W_CASE)


def _nouns_after(text: str, end: int) -> tuple[Noun, ...]:
    word, end = _next_word(text, end)
    if word is None or word in _TIME_OF_DAY or word in _NOT_NOUNS:
        return ()
    if word in _ABBREVIATIONS:
        return _abbreviation(word)
    pos, nouns = _word(word)
    if pos in ("ADJF", "PRTF"):  # "2 новые машины": the noun after the adjective
        word, _ = _next_word(text, end)
        if word is None:
            return ()
        pos, nouns = _word(word)
    return nouns


def _count_fits(n: int, case: str, noun: Noun) -> bool:
    """Whether ``noun`` is the form a count ``n`` in ``case`` takes."""
    if case in ("nomn", "accs"):
        if _one(n):
            return not noun.plural and noun.case == case
        if _few(n):
            # "2 минуты"; an animate accusative: "вижу 2 друзей".
            return ((not noun.plural and noun.case == "gent")
                    or (case == "accs" and noun.animate and noun.plural and noun.case in ("gent", "accs")))
        return noun.plural and noun.case == "gent"
    return noun.plural != _one(n) and noun.case == case


def _reading(n: int, prep: str | None, nouns: tuple[Noun, ...], before: str | None = None) -> Reading:
    """How to read ``n`` after the preposition ``prep`` (or the word
    ``before`` it) and before ``nouns``, the readings of the next word."""
    cases = _cases(prep)

    def governed(noun: Noun) -> bool:
        """Whether the noun's case can be the one the number is in."""
        return noun.case in cases if prep else noun.case in ("nomn", "accs", "gent")

    for noun in nouns:
        if noun.plural or not governed(noun):
            continue
        # "в 2024 году", "к 21 году", "2024 года": a year, not years.
        if noun.lemma == "год" and (n >= 1000 or noun.case in _OBLIQUE):
            return Reading(True, noun.case)
        # "1 место", "на 1 месте": first, not one.
        if _one(n) and noun.lemma in _ORDINAL_ONE:
            return Reading(True, noun.case, noun.gender or "masc")
    for case in cases:
        for noun in nouns:
            if _count_fits(n, case, noun):
                return Reading(False, case, noun.gender or "masc",
                               animate=case == "accs" and noun.animate)
    # A singular noun a count cannot take: an ordinal that agrees with it.
    # (A plural one is more often a misread: "1.5к голды" is no "пятисотые".)
    for noun in nouns:
        if noun.plural or not governed(noun):
            continue
        if noun.lemma in _ORDINAL_NOUNS or (prep and noun.case in _OBLIQUE and not noun.animate):
            return Reading(True, noun.case, noun.gender or "masc",
                           animate=noun.case == "accs" and noun.animate)
    # A plural genitive no count takes is governed by the word before it:
    # "нет 2 рублей".
    if (before in _GENITIVE_WORDS and not _one(n)
            and any(noun.plural and noun.case == "gent" for noun in nouns)):
        return Reading(False, "gent")
    if prep in ("в", "во") and 1900 <= n <= 2100 and not nouns:  # "в 2020": a year
        return Reading(True, "loct")
    if prep == "по" and _one(n):  # "по 1" -> "по одному"
        return Reading(False, "datv")
    return Reading(False, cases[0])


def _pad(text: str, start: int, end: int, words: str) -> str:
    """``words`` in place of text[start:end], spaced off from adjacent letters
    ("CS2" -> "CS два")."""
    if start > 0 and text[start - 1].isalpha():
        words = " " + words
    if end < len(text) and text[end].isalpha():
        words += " "
    return words


# -- passes ------------------------------------------------------------
# Each pass rewrites one written form into words; later passes no longer
# see its digits. The order matters: dates before decimals and versions,
# shorthand before plain numbers.

_SIGN = r"(?:(?<![^\s(])([+\-−]))?"
_NUM = r"([0-9]+(?:[.,][0-9]+)?)"

_LONG_RUN = re.compile(rf"{_SIGN}(?<![0-9])([0-9]{{10,}})(?![0-9])")
# Thousands written apart ("1 000 000", "2 500"), up to 15 digits; whether
# the groups are one number or a list is decided in _join_groups.
_GROUP_SPACE = r"[ \xa0\N{NARROW NO-BREAK SPACE}]"
_GROUPS = re.compile(rf"(?<![0-9.,])([0-9]{{1,3}})((?:{_GROUP_SPACE}[0-9]{{3}}){{1,4}})(?![0-9]|[.,][0-9])")
_AMOUNT_AFTER = re.compile(r"[ ]?(?:[%$€£₽₴]|руб|р\b|тыс|млн|млрд)", re.I)
_DAY_RANGE_BY_PREP = re.compile(
    rf"(?<![А-Яа-яЁё])(с|со|от)[ ]([0-9]{{1,2}})[ ](по|до)[ ]([0-9]{{1,2}})[ ]({_MONTH})\b", re.I)
_DAY_RANGE = re.compile(rf"(?<![0-9.,])([0-9]{{1,2}})[ ]?[-–—][ ]?([0-9]{{1,2}})[ ]({_MONTH})\b", re.I)
_DAY_MONTH = re.compile(rf"(?<![0-9.,])([0-9]{{1,2}})(?:-?(?:го|е|ое))?[ ]({_MONTH})\b", re.I)
# A date is not followed by a unit: "5.10$", "1.05%", "1.5к" are amounts.
_NUMERIC_DATE = re.compile(
    r"(?<![0-9.,])([0-9]{1,2})([./])([0-9]{1,2})(?:\2([0-9]{4}|[0-9]{2}))?(?![0-9]|[./][0-9]|[%$€£₽₴кkК])")
_ISO_DATE = re.compile(r"(?<![0-9.,\-])([0-9]{4})-([0-9]{2})-([0-9]{2})(?![0-9]|-[0-9])")
_VERSION = re.compile(r"(?<![0-9.,])[0-9]+(?:\.[0-9]+){2,}(?![0-9])")
_PHONE = re.compile(r"(?<![0-9.,\-])[0-9]+(?:-[0-9]+){2,}(?![0-9])")
_TIME = re.compile(r"(?<![0-9.,:])([01]?[0-9]|2[0-3]):([0-5][0-9])(?![0-9]|:[0-9])")
_SCORE = re.compile(r"(?<![0-9.,:])([0-9]+):([0-9]+)(?![0-9]|:[0-9])")
_THOUSANDS = re.compile(rf"(?<![0-9.,]){_NUM}(кк|kk|к|k|К)(?![А-Яа-яЁёA-Za-z0-9])")
_MULTIPLIER = re.compile(
    rf"(?<![0-9.,]){_NUM}[ ]?(тыс|млн|млрд|трлн)\.?(?![А-Яа-яЁё])(?:[ ]?([$€£₽₴]|руб\.?|р\.?)(?![А-Яа-яЁёA-Za-z0-9]))?",
    re.I)
_MONEY_BEFORE = re.compile(rf"([$€£₽₴])[ ]?{_NUM}(?![0-9])")
_MONEY_AFTER = re.compile(rf"(?<![0-9.,]){_NUM}[ ]?(%|[$€£₽₴]|руб\.?|р\.?)(?![А-Яа-яЁёA-Za-z0-9])", re.I)
_BY = re.compile(r"(?<![0-9.,])([0-9]+)[ ]?[xXхХ×vV][ ]?([0-9]+)(?![0-9])")
_TIMES = re.compile(rf"(?<![А-Яа-яЁёA-Za-z0-9])[xXхХ×]{_NUM}(?![0-9])")
_FRACTION = re.compile(r"(?<![0-9.,/])([0-9]+)/([0-9]+)(?![0-9/])")
_NUMBER_SIGN = re.compile(r"[№#][ ]?([0-9]+)(?![0-9])")
_SUFFIXES = ("ыми", "ими", "ого", "его", "ому", "ему", "ый", "ий", "ой", "ей", "ая", "ья", "ое",
             "ье", "ые", "ьи", "ых", "их", "ым", "им", "ую", "ью", "ом", "ем", "го", "му", "ми",
             "ти", "ух", "ёх", "ех", "мя", "й", "я", "е", "ю", "м", "х")
_HYPHENLESS = ("ого", "ому", "ый", "ий", "ой", "ая", "ое", "ые", "ых", "ым", "ую", "ом", "го",
               "ти", "ух")
_SUFFIXED = re.compile(
    rf"(?<![0-9.,])([0-9]+)(?:[-–]({'|'.join(_SUFFIXES)})|({'|'.join(_HYPHENLESS)}))(?![А-Яа-яЁё])",
    re.I)
_RANGE = re.compile(rf"{_SIGN}(?<![0-9.,:/])([0-9]+)[ ]?[-–—][ ]?([0-9]+)(?![0-9.,:/])")
_DECIMAL = re.compile(rf"{_SIGN}(?<![0-9.,])([0-9]+)([.,])([0-9]+)(?![0-9]|[.,][0-9])")
_INTEGER = re.compile(rf"{_SIGN}(?<![0-9])([0-9]+)(?![0-9])")

# Ordinal readings a written ending allows: (case, gender, plural).
_M, _F, _N = "masc", "femn", "neut"
_ORDINAL_ENDINGS: dict[str, tuple[tuple[str, str, bool], ...]] = {}
for _ends, _readings in (
    (("й", "ой"), (("nomn", _M, False), ("accs", _M, False), ("gent", _F, False), ("datv", _F, False),
                  ("ablt", _F, False), ("loct", _F, False))),
    (("ей",), (("gent", _F, False), ("datv", _F, False), ("ablt", _F, False), ("loct", _F, False))),
    (("ьи",), (("nomn", _M, True), ("accs", _M, True))),
    (("ый", "ий"), (("nomn", _M, False), ("accs", _M, False))),
    (("я", "ая", "ья"), (("nomn", _F, False),)),
    (("е", "ое", "ье"), (("nomn", _N, False), ("accs", _N, False), ("nomn", _M, True), ("accs", _M, True))),
    (("ые",), (("nomn", _M, True), ("accs", _M, True))),
    (("ю", "ую", "ью"), (("accs", _F, False),)),
    (("го", "ого", "его"), (("gent", _M, False), ("gent", _N, False))),
    (("му", "ому", "ему"), (("datv", _M, False), ("datv", _N, False))),
    (("м",), (("ablt", _M, False), ("loct", _M, False), ("ablt", _N, False), ("loct", _N, False),
              ("datv", _M, True))),
    (("ым", "им"), (("ablt", _M, False), ("ablt", _N, False), ("datv", _M, True))),
    (("ом", "ем"), (("loct", _M, False), ("loct", _N, False))),
    (("ми", "ыми", "ими"), (("ablt", _M, True),)),
    (("х", "ых", "их"), (("gent", _M, True), ("loct", _M, True))),
):
    for _end in _ends:
        _ORDINAL_ENDINGS[_end] = _readings
# Endings written after a cardinal: "5-ти", "7-ми", "2-х", "2-м", "5-ю".
_CARDINAL_ENDING_CASES = {"ти": ("gent", "datv", "loct"), "ми": ("gent", "datv", "loct"),
                          "х": ("gent", "loct"), "ух": ("gent", "loct"), "ёх": ("gent", "loct"),
                          "ех": ("gent", "loct"),
                          "м": ("datv",), "мя": ("ablt",), "ю": ("ablt",), "ью": ("ablt",),
                          "го": ("gent",), "й": ("gent", "datv", "ablt", "loct")}


def _suffixed(match: re.Match[str]) -> str:
    text = match.string
    n = int(match.group(1))
    ending = (match.group(2) or match.group(3)).lower()
    prep = _preposition(text, match.start())
    nouns = _nouns_after(text, match.end())
    cases = _cases(prep) if prep else ()

    def score(reading: Reading, fits: bool) -> int:
        points = 4 if fits else 0
        if prep:
            points += 2 if reading.case in cases else -2
        return points

    options: list[tuple[int, int, Reading]] = []
    decade = n % 10 == 0 and n >= 20
    for order, (case, gender, plural) in enumerate(_ORDINAL_ENDINGS.get(ending, ())):
        reading = Reading(True, case, gender, plural)
        agrees = any(noun.case == case and noun.plural == plural
                     and (plural or noun.gender in (None, gender)) for noun in nouns)
        bonus = 1 if decade == plural else 0  # "в 90-е": the nineties
        options.append((score(reading, agrees) + bonus, -order, reading))
    # A cardinal fits the ending only if its spelling ends with it:
    # "2-м" is "двум", "5-м" is not "пяти". Which endings a cardinal can
    # have follows from its last digits, which spares spelling the rest.
    if ending in ("й", "го"):
        possible = _one(n)
    elif ending in ("м", "х", "ух", "ёх", "ех", "мя"):
        possible = _few(n)
    else:
        possible = not (_one(n) or _few(n))
    written = ending.replace("ё", "е")  # "3-ех" is "трёх"
    for order, case in enumerate(_CARDINAL_ENDING_CASES.get(ending, ()) if possible else ()):
        for gender in ("masc", "femn"):
            if not cardinal(n, case, gender).replace("ё", "е").endswith(written):
                continue
            reading = Reading(False, case, gender)
            fits = any(_count_fits(n, case, noun) for noun in nouns)
            # "2-х", "7-ми", "5-ти" are cardinals; "1-й", "21-го" ordinals.
            prefer = (ending in ("ти", "ми", "ух", "ёх", "ех", "мя")
                      or (_few(n) and ending in ("х", "м")))
            options.append((score(reading, fits) + (2 if prefer else 0), -order - 10, reading))
    if not options:
        return match.group(0)
    best = max(options, key=lambda option: option[:2])[2]
    return _pad(text, match.start(), match.end(), _spell(n, best))


def _long_run(match: re.Match[str]) -> str:  # a phone number, an id
    words = _sign(match.group(1)) + _digits(match.group(2))
    return _pad(match.string, match.start(), match.end(), words)


def _date_case(text: str, start: int) -> str:
    prep = _preposition(text, start)
    if prep in ("к", "ко"):
        return "datv"
    if prep in ("на", "по", "в", "во", "за", "через"):
        return "accs"
    return "gent"  # "5 мая встречаемся", "до 30.10"


def _day(day: str, case: str) -> str:
    return ordinal(int(day), case, "neut")


def _day_range_by_prep(match: re.Match[str]) -> str:
    first, until = match.group(1), match.group(3)
    second_case = "accs" if until.lower() == "по" else "gent"
    return (f"{first} {_day(match.group(2), 'gent')} {until} "
            f"{_day(match.group(4), second_case)} {match.group(5)}")


def _day_range(match: re.Match[str]) -> str:
    case = _date_case(match.string, match.start())
    words = f"{_day(match.group(1), case)}-{_day(match.group(2), case)} {match.group(3)}"
    return _pad(match.string, match.start(), match.end(), words)


def _day_month(match: re.Match[str]) -> str:
    if not 1 <= int(match.group(1)) <= 31:
        return match.group(0)
    case = _date_case(match.string, match.start())
    return _pad(match.string, match.start(), match.end(), f"{_day(match.group(1), case)} {match.group(2)}")


def _date(text: str, start: int, end: int, day: str, month: str, year: str | None) -> str:
    words = f"{_day(day, _date_case(text, start))} {_MONTHS[int(month) - 1]}"
    if year is not None:
        words += f" {ordinal(int(year), 'gent')} года"
    return _pad(text, start, end, words)


def _numeric_date(match: re.Match[str]) -> str:
    text = match.string
    day, sep, month, year = match.group(1), match.group(2), match.group(3), match.group(4)
    if not (1 <= int(day) <= 31 and 1 <= int(month) <= 12):
        return match.group(0)  # "3.5", "1.15": decimals
    if year is None:
        # "30.10", "до 1.11" are dates; "1.12" is a decimal or a version,
        # "10/10" a score.
        if sep == "/" or len(month) != 2 or (
                len(day) != 2 and _word_before(text, match.start()) not in _DATE_PREPOSITIONS):
            return match.group(0)
    return _date(text, match.start(), match.end(), day, month, year)


def _iso_date(match: re.Match[str]) -> str:  # "2026-10-30"
    year, month, day = match.group(1), match.group(2), match.group(3)
    if not (1 <= int(day) <= 31 and 1 <= int(month) <= 12):
        return match.group(0)
    return _date(match.string, match.start(), match.end(), day, month, year)


def _version(match: re.Match[str]) -> str:
    text = match.string
    parts = match.group(0).split(".")
    if _one_number(parts[0], parts[1:], text, match.end()):
        # "1.000.000 рублей": thousands with dots
        n = int("".join(parts))
        words = _spell(n, _reading(n, _preposition(text, match.start()), _nouns_after(text, match.end())))
    else:
        words = " точка ".join(_integer(part) for part in parts)
    return _pad(text, match.start(), match.end(), words)


def _phone(match: re.Match[str]) -> str:
    words = " ".join(_integer(part) for part in match.group(0).split("-"))
    return _pad(match.string, match.start(), match.end(), words)


def _context_case(text: str, start: int) -> str:
    """The case of a number with no noun to agree with: its preposition's
    ("на 1,5 часа" -> "на одну целую пять десятых")."""
    prep = _preposition(text, start)
    return PREPOSITIONS[prep][0] if prep else "nomn"


def _time(match: re.Match[str]) -> str:
    case = _context_case(match.string, match.start())
    hours, minutes = int(match.group(1)), int(match.group(2))
    if minutes == 0:
        tail = "ноль-ноль"
    elif minutes < 10:
        tail = f"ноль {cardinal(minutes)}"
    else:
        tail = cardinal(minutes, case)
    return _pad(match.string, match.start(), match.end(), f"{cardinal(hours, case)} {tail}")


def _score(match: re.Match[str]) -> str:
    words = f"{_integer(match.group(1))}-{_integer(match.group(2))}"
    return _pad(match.string, match.start(), match.end(), words)


def _thousands(match: re.Match[str]) -> str:
    text = match.string
    scale = 1_000_000 if len(match.group(2)) == 2 else 1000
    value = Decimal(match.group(1).replace(",", ".")) * scale
    prep = _preposition(text, match.start())
    nouns = _nouns_after(text, match.end())
    if value != value.to_integral_value():
        words = f"{_number(match.group(1), _context_case(text, match.start()))[0]} " \
                f"{_counted('тысяча' if scale == 1000 else 'миллион', None, 'gent')}"
    elif scale == 1000 and any(not noun.plural and noun.case in ("nomn", "accs") for noun in nouns) \
            and not any(_count_fits(int(value), case, noun) for case in _cases(prep) for noun in nouns):
        # "4к монитор": a resolution, not four thousand monitors.
        words = f"{_number(match.group(1))[0]} ка"
    else:
        n = int(value)
        reading = _reading(n, prep, nouns)
        words = _spell(n, reading._replace(ordinal=False) if reading.ordinal else reading)
    return _pad(text, match.start(), match.end(), words)


def _multiplier(match: re.Match[str]) -> str:
    text = match.string
    case = _context_case(text, match.start())
    lemma = _MULTIPLIERS.get(match.group(2).lower())
    if lemma is None:  # re.I matched an old Cyrillic letter (U+1C80..U+1C88)
        return match.group(0)
    gender = _gender_of(lemma)
    number, n = _number(match.group(1), case, gender)
    words = f"{number} {_counted(lemma, n, case)}"
    currency = _currency(match.group(3)) if match.group(3) else None
    if currency is not None:
        words += " " + _counted(currency, 5, "nomn")  # "миллиона рублей"
    return _pad(text, match.start(), match.end(), words)


def _currency(symbol: str) -> str | None:
    return _CURRENCIES.get(symbol.lower().rstrip("."))


def _amount(text: str, start: int, end: int, amount: str, unit: str) -> str:
    case = _context_case(text, start)
    lemma = "процент" if unit == "%" else _currency(unit)
    if lemma is None:
        return text[start:end]
    number, n = _number(amount, case, _gender_of(lemma))
    return _pad(text, start, end, f"{number} {_counted(lemma, n, case)}")


def _money_before(match: re.Match[str]) -> str:
    return _amount(match.string, match.start(), match.end(), match.group(2), match.group(1))


def _money_after(match: re.Match[str]) -> str:
    return _amount(match.string, match.start(), match.end(), match.group(1), match.group(2))


def _by(match: re.Match[str]) -> str:
    words = f"{_integer(match.group(1))} на {_integer(match.group(2))}"
    return _pad(match.string, match.start(), match.end(), words)


def _times(match: re.Match[str]) -> str:
    return _pad(match.string, match.start(), match.end(), f"икс {_number(match.group(1))[0]}")


def _fraction(match: re.Match[str]) -> str:
    a, b = int(match.group(1)), int(match.group(2))
    if a > b or a == b == 50:  # "24/7", "50/50"
        words = f"{cardinal(a)} на {cardinal(b)}"
    elif b in (2, 3, 4) and 0 < a < b:  # "1/2", "2/3"
        words = (f"{cardinal(a, gender='femn')} "
                 f"{ordinal(b, 'nomn', 'femn') if _one(a) else ordinal(b, 'gent', plural=True)}")
    else:  # "10/10", "3/5"
        words = f"{cardinal(a)} из {cardinal(b, 'gent')}"
    return _pad(match.string, match.start(), match.end(), words)


def _number_sign(match: re.Match[str]) -> str:
    return _pad(match.string, match.start(), match.end(), f"номер {_integer(match.group(1))}")


def _range(match: re.Match[str]) -> str:
    text = match.string
    a, b = match.group(2), match.group(3)
    prep = _preposition(text, match.start())
    reading = _reading(int(b), prep, _nouns_after(text, match.end()))
    words = f"{_sign(match.group(1))}{_as_read(a, reading)}-{_as_read(b, reading)}"
    return _pad(text, match.start(), match.end(), words)


def _as_read(text: str, reading: Reading) -> str:
    if len(text) > 1 and text[0] == "0":
        return _digits(text)
    n = int(text)
    if not reading.ordinal and reading.gender == "femn" and not (_one(n) or _few(n)):
        reading = reading._replace(gender="masc")
    return _spell(n, reading)


def _decimal_number(match: re.Match[str]) -> str:
    text = match.string
    whole, sep, frac = match.group(2), match.group(3), match.group(4)
    start = match.start(2)
    if sep == "." and (_word_before(text, start) in _VERSION_WORDS or text[start - 1:start] in ("v", "V")):
        words = f"{_integer(whole)} точка {_integer(frac)}"  # "патч 1.12", "v1.2"
    elif sep == "." and frac == "000":  # "1.000 рублей": a thousand
        n = int(whole) * 1000
        words = _spell(n, _reading(n, _preposition(text, start), _nouns_after(text, match.end())))
    else:
        words = _decimal(whole, frac, sep, _context_case(text, start))
    return _pad(text, match.start(), match.end(), _sign(match.group(1)) + words)


def _integer_number(match: re.Match[str]) -> str:
    text = match.string
    digits = match.group(2)
    if len(digits) > 1 and digits[0] == "0":
        words = _digits(digits)
    else:
        n = int(digits)
        prep = _preposition(text, match.start())
        word, _ = _next_word(text, match.end())
        if n == 1 and word in _TIME_OF_DAY:  # "в 1 ночи" -> "в час ночи"
            words = _counted("час", 1, _cases(prep)[0] if prep else "nomn")
        else:
            reading = _reading(n, prep, _nouns_after(text, match.end()), _word_before(text, match.start()))
            words = _spell(n, reading)
    return _pad(text, match.start(), match.end(), _sign(match.group(1)) + words)


# Two-digit groups after a run of digit groups: a phone number ("8 800 555 35 35").
_PHONE_TAIL = re.compile(r"[ \xa0.\-][0-9]{2}(?![0-9])")


def _one_number(head: str, groups: list[str], text: str, end: int) -> bool:
    """Whether digit groups written apart ("1 250", "1.000.000") are one
    number: thousands of up to 15 digits that are not a list of numbers
    ("144 165 240 гц"), a phone number or an address ("192.168.100.200")."""
    if not 1 <= len(groups) <= 4 or len(head) > 3 or any(len(group) != 3 for group in groups):
        return False
    if all(group.endswith("00") for group in groups) or _AMOUNT_AFTER.match(text, end):
        return True
    # A head of one or two digits is a number of thousands: "1 250 подписчиков".
    return len(head) <= 2 and not _PHONE_TAIL.match(text, end)


def _join_groups(match: re.Match[str]) -> str:
    groups = re.split(_GROUP_SPACE, match.group(2))[1:]
    if _one_number(match.group(1), groups, match.string, match.end()):
        return match.group(1) + "".join(groups)
    return match.group(0)


_PASSES = (
    (_LONG_RUN, _long_run),
    (_GROUPS, _join_groups),
    (_DAY_RANGE_BY_PREP, _day_range_by_prep),
    (_DAY_RANGE, _day_range),
    (_DAY_MONTH, _day_month),
    (_NUMERIC_DATE, _numeric_date),
    (_ISO_DATE, _iso_date),
    (_VERSION, _version),
    (_PHONE, _phone),
    (_TIME, _time),
    (_SCORE, _score),
    (_THOUSANDS, _thousands),
    (_MULTIPLIER, _multiplier),
    (_MONEY_BEFORE, _money_before),
    (_MONEY_AFTER, _money_after),
    (_BY, _by),
    (_TIMES, _times),
    (_FRACTION, _fraction),
    (_NUMBER_SIGN, _number_sign),
    (_SUFFIXED, _suffixed),
    (_RANGE, _range),
    (_DECIMAL, _decimal_number),
    (_INTEGER, _integer_number),
)


def _english(clause: str, needed: int) -> bool:
    words = _LATIN_WORD.findall(clause.lower())
    english = sum(word in _ENGLISH_WORDS for word in words)
    return english >= needed and 2 * english >= len(words)


def _hide_english(match: re.Match[str], needed: int) -> str:
    stretch = match.group(0)
    kept = ""
    if match.start() > 0:
        # A number right after a Russian word is Russian: "в 21:00 all in".
        latin = _LATIN.search(stretch)
        cut = latin.start() if latin else len(stretch)
        kept, stretch = stretch[:cut], stretch[cut:]
    return kept + "".join(clause.translate(_HIDE) if _english(clause, needed) else clause
                          for clause in _CLAUSE_END.split(stretch))


# "в втором" -> "во втором", "с второй" -> "со второй", "с ста" -> "со ста".
_EUPHONY = re.compile(r"(?<![А-Яа-яЁё])(?:([ВвКкСс])(?=[ \xa0]+втор)|([Сс])(?=[ \xa0]+ст[ао]\b))")


def speak_numbers(text: str) -> str:
    """``text`` with its numbers in Russian words, except in English text."""
    if not _DIGIT.search(text):
        return text
    hidden = False
    if _LATIN.search(text):
        shown = text
        needed = 2 if _CYRILLIC.search(text) else 1
        text = _NON_CYRILLIC.sub(lambda match: _hide_english(match, needed), text)
        hidden = text != shown
        if not _DIGIT.search(text):
            return shown
    for pattern, replace in _PASSES:
        text = pattern.sub(replace, text)
    text = _EUPHONY.sub(lambda match: match.group(0) + "о", text)
    return text.translate(_SHOW) if hidden else text
