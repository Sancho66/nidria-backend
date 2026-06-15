"""Collectable CASE-LEVEL fields (option b) — the source of truth for
which `client_case` columns a journey may collect at case creation.

Distinct from `COLLECTABLE_BASE_FIELDS` (person fields on case_person):
these are attributes of the FILE itself, kept on `client_case`, and they
NEVER move from there. A journey declares it collects one of these via
`journey_template_case_field`; the value is written through the existing
top-level create keys. Extend this set (street/city/postal…) without a
new model — just add the column reference here."""

# The country columns on client_case. Mirrors the country query
# ecosystem (filters/sorts/dashboard/views) which is left untouched.
COLLECTABLE_CASE_FIELDS: frozenset[str] = frozenset(
    {
        "origin_country",
        "dest_country",
    }
)
