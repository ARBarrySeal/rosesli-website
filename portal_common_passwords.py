"""Common-password block list.

A curated set of ~150 of the most-reused weak passwords drawn from
publicly-disclosed breach corpora (RockYou, HIBP top releases, NIST
SP800-63B reference lists). Held as a frozenset for O(1) membership
checks during password validation.

This list intentionally stays small enough to inline rather than ship as
a 50 KB+ file: covering the long tail of breach data has diminishing
returns once minimum-length and character-class rules apply. The 12-char
minimum already disqualifies most short reused passwords; this list
exists to catch the predictable "Password1234!" / "Welcome2024!" pattern
where a user satisfies the rules with a known-bad base word + suffix.

A trailing-digits / common-symbol strip happens in _password_too_weak
before membership check, so "Password123!" → "password" → rejected.
"""

COMMON_PASSWORDS: frozenset[str] = frozenset({
    # Numeric-only sequences (caught after suffix strip when used as base)
    "123456", "12345678", "123456789", "1234567", "12345", "1234567890",
    "111111", "000000", "666666", "121212", "987654321", "654321", "112233",

    # Keyboard walks
    "qwerty", "qwertyuiop", "asdfghjkl", "zxcvbnm", "qazwsx", "1qaz2wsx",
    "1q2w3e4r", "1q2w3e", "qwerty12", "qwerty123", "qweasd", "asdf",

    # The classic English words
    "password", "passw0rd", "p@ssword", "p@ssw0rd", "pass", "passwd",
    "welcome", "letmein", "login", "admin", "administrator", "root",
    "guest", "user", "test", "demo", "default", "changeme", "secret",

    # Affirmations / pop words
    "iloveyou", "trustno1", "sunshine", "princess", "superman", "batman",
    "starwars", "freedom", "monkey", "dragon", "shadow", "master", "killer",

    # Sports
    "football", "baseball", "basketball", "soccer", "hockey", "lakers",
    "yankees", "raiders", "cowboys", "celtic", "arsenal", "liverpool",

    # Names + common pet/family
    "michael", "jennifer", "jessica", "ashley", "michelle", "matthew",
    "daniel", "andrew", "joshua", "robert", "charlie", "thomas",
    "anthony", "william", "richard", "joseph", "samantha", "nicole",
    "amanda", "hannah", "taylor", "tigger", "buster", "cookie",

    # Tech / corporate
    "computer", "internet", "google", "facebook", "microsoft", "windows",
    "apple", "linux", "ubuntu", "centos", "oracle", "android",

    # Years (caught when used as base or with year suffix)
    "2024", "2025", "2026", "2023", "2022", "2021", "2020",

    # Common + suffix stems (the trailing-strip catches "<base>123",
    # but some bases are themselves common with year-like suffixes)
    "summer", "winter", "spring", "autumn", "january", "february",
    "march", "april", "august", "september", "october", "november",
    "december",

    # Profanity / "clever" choices
    "fuckyou", "asshole", "bitch", "dickhead", "abc123", "qwer1234",

    # Phone-keypad / 12-char weak-but-meets-criteria
    "P@ssword1", "Password1", "Password123", "Welcome1", "Welcome123",
    "Admin1234", "Letmein123", "Changeme1", "Qwerty123", "Password!",

    # Sequences + words combined
    "abcdef", "abcdefg", "abcabc", "asdasd", "qweqwe",
})
