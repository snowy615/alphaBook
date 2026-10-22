"""
The Approx question bank: 50 estimation questions a host can pick from.

Not trivia you either remember or don't: each one can be reasoned to the
right order of magnitude from things most people roughly know (a Fermi
estimate, or a bit of maths or probability). The real-world answers are
well documented, the maths ones exact (each was computed, not recalled).
Every answer is positive, as Estimathon intervals must be. ``note`` is shown
with the answer once the game is over: how you might have got there.
"""
from typing import Dict, List

BANK: List[Dict] = [
    # ── Fermi estimates: the built world ────────────────────────────────
    {"id": "eiffel-rivets", "text": "Number of rivets holding the Eiffel Tower together", "answer": 2_500_000,
     "note": "About 18,000 iron parts, each fixed with on the order of a hundred rivets."},
    {"id": "eiffel-mass", "text": "Mass of the Eiffel Tower's iron structure, in tonnes", "answer": 7300,
     "note": "Mostly air: melted down, the iron would fill its 125 m square base only 6 cm deep."},
    {"id": "eiffel-steps", "text": "Number of steps from the ground to the top of the Eiffel Tower", "answer": 1665,
     "note": "About 300 m of climb at roughly 18 cm per step."},
    {"id": "opera-tiles", "text": "Number of tiles on the roof of the Sydney Opera House", "answer": 1_056_006,
     "note": "Around 1.6 hectares of roof shells, covered in tiles about 12 cm across."},
    {"id": "empire-windows", "text": "Number of windows in the Empire State Building", "answer": 6514,
     "note": "102 storeys, four faces, a few dozen windows per face per floor, fewer near the top."},
    {"id": "palace-rooms", "text": "Number of rooms in Buckingham Palace", "answer": 775,
     "note": "Including 188 staff bedrooms, 92 offices and 78 bathrooms."},
    {"id": "pyramid-blocks", "text": "Number of stone blocks in the Great Pyramid of Giza", "answer": 2_300_000,
     "note": "About 2.6 million cubic metres, at roughly a cubic metre a block."},
    {"id": "olympic-pool", "text": "Litres of water in an Olympic swimming pool", "answer": 2_500_000,
     "note": "50 m × 25 m × 2 m = 2,500 m³, and a cubic metre is 1,000 litres."},
    {"id": "tube-length", "text": "Total length of the London Underground network, in km", "answer": 402,
     "note": "11 lines, the longest (Central) about 74 km."},
    {"id": "iss-mass", "text": "Mass of the International Space Station, in kg", "answer": 420_000,
     "note": "Roughly a fully loaded 747, assembled over more than 40 launches."},
    {"id": "m1-transistors", "text": "Number of transistors on Apple's M1 chip (2020)", "answer": 16e9,
     "note": "Moore's law: from the Intel 4004's 2,300 in 1971, doubling about every 2 years and 2 months."},
    {"id": "monopoly-money", "text": "Total money, in dollars, in a standard US Monopoly set (2008 onwards)", "answer": 20_580,
     "note": "30 of each note: $500, $100, $50, $20, $10, $5 and $1, so 30 × $686."},

    # ── Fermi estimates: people and places ──────────────────────────────
    {"id": "heathrow-2023", "text": "Number of passengers through Heathrow in 2023", "answer": 79.2e6,
     "note": "About 1,300 flights a day, around 170 passengers each."},
    {"id": "starbucks", "text": "Number of Starbucks stores worldwide (end of 2023)", "answer": 38_038,
     "note": "About 16,000 in the US and 7,000 in China, plus the rest of the world."},
    {"id": "mcdonalds", "text": "Number of McDonald's restaurants worldwide (end of 2023)", "answer": 41_822,
     "note": "Roughly one for every 200,000 people on Earth, far denser in rich countries."},
    {"id": "louvre-2023", "text": "Number of visitors to the Louvre in 2023", "answer": 8.9e6,
     "note": "About 30,000 a day, open around 300 days a year."},
    {"id": "uk-tea", "text": "Cups of tea drunk in the UK each day", "answer": 100e6,
     "note": "67 million people, about half drinking around three cups a day."},
    {"id": "humans-ever", "text": "Number of humans who have ever been born", "answer": 117e9,
     "note": "Today's 8 billion are only about 7% of everyone who has ever lived."},
    {"id": "trees", "text": "Number of trees on Earth", "answer": 3.04e12,
     "note": "About 400 for every person; forests cover roughly 4 billion hectares."},
    {"id": "gold-mined", "text": "Total gold ever mined, in tonnes (to end of 2023)", "answer": 212_582,
     "note": "All of it would make a cube only about 22 m along each side."},
    {"id": "amazon-flow", "text": "Average flow of the Amazon River into the sea, in cubic metres per second", "answer": 209_000,
     "note": "About a fifth of all the fresh water reaching the world's oceans."},

    # ── Fermi estimates: the body and books ─────────────────────────────
    {"id": "neurons", "text": "Number of neurons in the human brain", "answer": 86e9,
     "note": "A 1.4 kg brain, packed with cells only microns across."},
    {"id": "body-cells", "text": "Number of human cells in an adult body", "answer": 3.0e13,
     "note": "Mostly red blood cells: about 25 trillion of them."},
    {"id": "dna-length", "text": "Length of the DNA in one human cell, stretched out, in metres", "answer": 2,
     "note": "6 billion base pairs, each 0.34 nanometres apart."},
    {"id": "kjv-words", "text": "Number of words in the King James Bible", "answer": 783_137,
     "note": "About 1,200 pages of roughly 650 words."},
    {"id": "hp-words", "text": "Total number of words in all seven Harry Potter books", "answer": 1_084_170,
     "note": "From about 77,000 words in the first book to 257,000 in the fifth."},
    {"id": "oed-pages", "text": "Number of pages in the 20-volume Oxford English Dictionary (2nd edition)", "answer": 21_728,
     "note": "Twenty volumes of about 1,100 pages each."},

    # ── Physics and space, worked out ───────────────────────────────────
    {"id": "earths-in-sun", "text": "Number of Earths that would fit inside the Sun, by volume", "answer": 1.306e6,
     "note": "The Sun's radius is about 109 Earths, and 109³ ≈ 1.3 million."},
    {"id": "orbit-per-day", "text": "Distance the Earth travels round the Sun in one day, in km", "answer": 2.573e6,
     "note": "2π × 150 million km in a year, divided by 365."},
    {"id": "water-molecules", "text": "Number of water molecules in a 250 ml glass of water", "answer": 8.357e24,
     "note": "250 g ÷ 18 g per mole ≈ 14 moles, × 6 × 10²³."},
    {"id": "paper-moon", "text": "Times you'd need to fold a 0.1 mm sheet of paper in half for it to reach the Moon", "answer": 42,
     "note": "Each fold doubles it: 0.1 mm × 2⁴² ≈ 440,000 km, past the Moon's 384,400 km."},
    {"id": "coin-stack", "text": "Height of a stack of one billion £1 coins, in km", "answer": 2800,
     "note": "A £1 coin is 2.8 mm thick."},
    {"id": "count-billion", "text": "Years it would take to count to one billion, one number a second, non-stop", "answer": 31.69,
     "note": "A year is about 31.6 million seconds."},
    {"id": "heartbeats", "text": "Heartbeats in an 80-year life at 70 beats a minute", "answer": 2.945e9,
     "note": "70 × 60 × 24 × 365 × 80."},

    # ── Counting and maths ──────────────────────────────────────────────
    {"id": "change-pound", "text": "Number of ways to make £1 from UK coins (1p, 2p, 5p, 10p, 20p, 50p, £1)", "answer": 4563,
     "note": "Mostly ways using lots of 1p and 2p coins; a classic dynamic-programming count."},
    {"id": "lattice-paths", "text": "Number of shortest paths from one corner of a 20 × 20 grid to the opposite corner", "answer": 137_846_528_820,
     "note": "Choose which 20 of the 40 steps go right: 40 choose 20."},
    {"id": "primes-million", "text": "Number of prime numbers below one million", "answer": 78_498,
     "note": "The prime number theorem: about n / ln n = 1,000,000 / 13.8 ≈ 72,000."},
    {"id": "zeros-1000", "text": "Number of zeros at the end of 1000! (1000 factorial)", "answer": 249,
     "note": "One per factor of 5: 200 + 40 + 8 + 1."},
    {"id": "digits-2-1000", "text": "Number of digits in 2 to the power 1000", "answer": 302,
     "note": "1000 × log₁₀2 ≈ 1000 × 0.301."},
    {"id": "digit-sum-2-1000", "text": "Sum of the digits of 2 to the power 1000", "answer": 1366,
     "note": "302 digits averaging about 4.5 each."},
    {"id": "mississippi", "text": "Number of different ways to arrange the letters of MISSISSIPPI", "answer": 34_650,
     "note": "11! ÷ (4! × 4! × 2!) for the repeated I, S and P."},
    {"id": "chess-rectangles", "text": "Number of rectangles of any size on a chessboard (squares included)", "answer": 1296,
     "note": "Pick 2 of the 9 vertical lines and 2 of the 9 horizontal ones: 36 × 36."},
    {"id": "tictactoe", "text": "Number of different possible games of noughts and crosses (move by move, to a win or a full board)", "answer": 255_168,
     "note": "Fewer than 9! = 362,880, because many games end early."},
    {"id": "compound-1pc", "text": "What £1 grows to after 365 days of 1% daily interest, in £", "answer": 37.78,
     "note": "1.01³⁶⁵ = e^(365 × ln 1.01) ≈ e^3.63."},
    {"id": "e-to-pi", "text": "e to the power π", "answer": 23.14,
     "note": "Not far from 20 + π, a famous near-coincidence."},

    # ── Probability ─────────────────────────────────────────────────────
    {"id": "dice-all-six", "text": "Expected number of rolls of a fair die until every face has come up", "answer": 14.7,
     "note": "6 × (1 + 1/2 + 1/3 + 1/4 + 1/5 + 1/6): the coupon collector's problem."},
    {"id": "birthday-23", "text": "Chance, in %, that at least two of 23 people share a birthday", "answer": 50.73,
     "note": "253 pairs of people, each with a 1-in-365 chance."},
    {"id": "birthday-wait", "text": "Expected number of people who enter a room before two share a birthday", "answer": 24.62,
     "note": "Roughly √(π × 365 / 2) + 2/3."},
    {"id": "first-ace", "text": "Expected number of cards dealt from a shuffled deck up to and including the first ace", "answer": 10.6,
     "note": "The 4 aces split the other 48 cards into 5 gaps of about 9.6: (52 + 1) ÷ (4 + 1)."},
    {"id": "poker-flush", "text": "Chance, in %, that a random 5-card poker hand is a flush (not a straight flush)", "answer": 0.1965,
     "note": "4 suits × (13 choose 5), less the 40 straight flushes, out of 2,598,960 hands."},
]

# For browsing the bank by theme on the host's page.
CATEGORIES: Dict[str, List[str]] = {
    "Built world": ["eiffel-rivets", "eiffel-mass", "eiffel-steps", "opera-tiles", "empire-windows", "palace-rooms",
                    "pyramid-blocks", "olympic-pool", "tube-length", "iss-mass", "m1-transistors", "monopoly-money"],
    "People & places": ["heathrow-2023", "starbucks", "mcdonalds", "louvre-2023", "uk-tea", "humans-ever", "trees",
                        "gold-mined", "amazon-flow"],
    "Body & books": ["neurons", "body-cells", "dna-length", "kjv-words", "hp-words", "oed-pages"],
    "Physics": ["earths-in-sun", "orbit-per-day", "water-molecules", "paper-moon", "coin-stack", "count-billion", "heartbeats"],
    "Maths": ["change-pound", "lattice-paths", "primes-million", "zeros-1000", "digits-2-1000", "digit-sum-2-1000",
              "mississippi", "chess-rectangles", "tictactoe", "compound-1pc", "e-to-pi"],
    "Probability": ["dice-all-six", "birthday-23", "birthday-wait", "first-ace", "poker-flush"],
}
for _category, _ids in CATEGORIES.items():
    for _q in BANK:
        if _q["id"] in _ids:
            _q["category"] = _category

BY_ID: Dict[str, Dict] = {q["id"]: q for q in BANK}
