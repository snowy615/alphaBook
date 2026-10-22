"""
The Approx question bank: 50 estimation questions a host can pick from.

Every answer is a positive number (Estimathon intervals can't include zero
or negatives) with a stable, well-documented value, so a round never hinges
on a disputed fact. Where a figure depends on a definition, the question
pins it down ("2021 census", "including all sizes", "at 20 °C").
"""
from typing import Dict, List

BANK: List[Dict] = [
    # ── Geography and the physical world ────────────────────────────────
    {"id": "everest", "text": "Height of Mount Everest above sea level, in metres", "answer": 8849},
    {"id": "thames", "text": "Length of the River Thames, in km", "answer": 346},
    {"id": "ben-nevis", "text": "Height of Ben Nevis, in metres", "answer": 1345},
    {"id": "wales-area", "text": "Area of Wales, in square kilometres", "answer": 20779},
    {"id": "earth-diameter", "text": "Diameter of the Earth at the equator, in km", "answer": 12756},
    {"id": "earth-circumference", "text": "Circumference of the Earth at the equator, in km", "answer": 40075},
    {"id": "burj-khalifa", "text": "Height of the Burj Khalifa, in metres", "answer": 828},
    {"id": "eiffel-built", "text": "Year the Eiffel Tower was completed", "answer": 1889},
    {"id": "africa-countries", "text": "Number of UN member states in Africa", "answer": 54},
    {"id": "un-members", "text": "Number of UN member states", "answer": 193},

    # ── Space and physics ───────────────────────────────────────────────
    {"id": "moon-distance", "text": "Average distance from the Earth to the Moon, in km", "answer": 384400},
    {"id": "sun-distance", "text": "Average distance from the Earth to the Sun, in km", "answer": 149_600_000},
    {"id": "light-speed", "text": "Speed of light in a vacuum, in km per second", "answer": 299792},
    {"id": "light-year", "text": "One light-year, in km", "answer": 9.461e12},
    {"id": "earth-mass", "text": "Mass of the Earth, in kg", "answer": 5.972e24},
    {"id": "universe-age", "text": "Age of the universe, in years", "answer": 1.38e10},
    {"id": "mars-year", "text": "Length of a year on Mars, in Earth days", "answer": 687},
    {"id": "sound-speed", "text": "Speed of sound in air at 20 °C, in metres per second", "answer": 343},
    {"id": "avogadro", "text": "Avogadro's number (particles in one mole)", "answer": 6.022e23},
    {"id": "elements", "text": "Number of elements in the periodic table", "answer": 118},

    # ── The human body ──────────────────────────────────────────────────
    {"id": "bones", "text": "Number of bones in the adult human body", "answer": 206},
    {"id": "chromosomes", "text": "Number of chromosomes in a typical human body cell", "answer": 46},

    # ── Oxford, London and the UK ───────────────────────────────────────
    {"id": "oxford-pop", "text": "Population of the city of Oxford at the 2021 census", "answer": 162100},
    {"id": "oxford-colleges", "text": "Number of Oxford colleges and permanent private halls", "answer": 39},
    {"id": "london-pop", "text": "Population of London at the 2021 census", "answer": 8_799_800},
    {"id": "tube-stations", "text": "Number of stations served by the London Underground", "answer": 272},
    {"id": "tube-opened", "text": "Year the London Underground opened", "answer": 1863},
    {"id": "commons-seats", "text": "Number of seats in the House of Commons", "answer": 650},
    {"id": "magna-carta", "text": "Year Magna Carta was sealed", "answer": 1215},

    # ── Finance ─────────────────────────────────────────────────────────
    {"id": "boe-founded", "text": "Year the Bank of England was founded", "answer": 1694},
    {"id": "nyse-founded", "text": "Year the New York Stock Exchange was founded (Buttonwood Agreement)", "answer": 1792},
    {"id": "bitcoin-supply", "text": "Maximum number of bitcoin that will ever exist", "answer": 21_000_000},
    {"id": "doubling-6pc", "text": "Years for money to double at 6% a year, compounded annually", "answer": 11.9},

    # ── Maths and counting ──────────────────────────────────────────────
    {"id": "chess-squares", "text": "Number of squares of all sizes on a chessboard", "answer": 204},
    {"id": "two-30", "text": "2 to the power 30", "answer": 1_073_741_824},
    {"id": "deck-orders", "text": "Number of ways to order a 52-card deck (52!)", "answer": 8.066e67},
    {"id": "primes-1000", "text": "Number of prime numbers below 1,000", "answer": 168},
    {"id": "digits-100-fact", "text": "Number of digits in 100! (100 factorial)", "answer": 158},
    {"id": "poker-hands", "text": "Number of distinct 5-card poker hands from a 52-card deck", "answer": 2_598_960},
    {"id": "rubiks", "text": "Number of possible positions of a Rubik's Cube", "answer": 4.325e19},
    {"id": "sum-1000", "text": "Sum of all whole numbers from 1 to 1,000", "answer": 500500},
    {"id": "e-10", "text": "e to the power 10", "answer": 22026.47},
    {"id": "trillion-seconds", "text": "One trillion seconds, in years", "answer": 31688},
    {"id": "year-seconds", "text": "Number of seconds in a (non-leap) year", "answer": 31_536_000},
    {"id": "week-minutes", "text": "Number of minutes in a week", "answer": 10080},

    # ── Everyday ────────────────────────────────────────────────────────
    {"id": "piano-keys", "text": "Number of keys on a standard piano", "answer": 88},
    {"id": "marathon", "text": "Length of a marathon, in metres", "answer": 42195},
    {"id": "scrabble-tiles", "text": "Number of tiles in an English-language Scrabble set", "answer": 100},
    {"id": "world-cup-2022", "text": "Number of matches played at the 2022 FIFA World Cup", "answer": 64},
    {"id": "iphone", "text": "Year the first iPhone was released", "answer": 2007},
]

BY_ID: Dict[str, Dict] = {q["id"]: q for q in BANK}
