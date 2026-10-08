"""The Email module: Lemlist campaigns sent to client profiles, ported from Prism.

Closed by default: the env master switch AND a per-agency row posed by a
superadmin (`agency_email_module`) open it, and only real effects, an
unmocked Lemlist, a key and an agency bound to the key's team make it talk
to Lemlist for real. Anything less is the dry run.
"""
