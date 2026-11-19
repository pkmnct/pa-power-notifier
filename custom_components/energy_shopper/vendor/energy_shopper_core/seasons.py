"""Calendar constants shared by usage projection, ranking and lock-in advice."""

AVG_DAYS_PER_MONTH = 30.4375
ELECTRIC_HIGH_USE_MONTHS = frozenset({6, 7, 8, 9})  # summer AC
GAS_HIGH_USE_MONTHS = frozenset({11, 12, 1, 2, 3})  # heating
MONTH_NAMES = {
    1: "January",
    2: "February",
    3: "March",
    4: "April",
    5: "May",
    6: "June",
    7: "July",
    8: "August",
    9: "September",
    10: "October",
    11: "November",
    12: "December",
}
