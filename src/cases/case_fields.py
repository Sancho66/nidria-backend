"""Collectable CASE-LEVEL fields (option b) — the source of truth for
which `client_case` columns a journey may collect at case creation.

Distinct from `COLLECTABLE_BASE_FIELDS` (person fields on case_person):
these are attributes of the FILE itself, kept on `client_case`, and they
NEVER move from there. A journey declares it collects one of these via
`journey_template_case_field`; the value is written through the existing
top-level create keys. Extend this set (street/city/postal…) without a
new model — just add the column reference here."""

# The address columns on client_case (origin + destination): country,
# street, city, postal_code. The country query ecosystem
# (filters/sorts/dashboard/views) is left untouched — street/city/postal
# are collectable only, never triable/filterable.
COLLECTABLE_CASE_FIELDS: frozenset[str] = frozenset(
    {
        "origin_country",
        "origin_street",
        "origin_city",
        "origin_postal_code",
        "dest_country",
        "dest_street",
        "dest_city",
        "dest_postal_code",
    }
)
