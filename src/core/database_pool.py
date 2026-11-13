"""Connection budget for one API process and its background workers.

Production Supavisor session mode accepts 15 clients. Independent default
SQLAlchemy pools could retain 5 each and open 10 extra each. Keep at most
11 pooled connections in total, leaving four for short-lived job engines,
migrations and diagnostics. Revisit this budget before adding API replicas.
"""

API_POOL_SIZE = 5
SCHEDULER_POOL_SIZE = 5
EMAIL_MONITOR_POOL_SIZE = 1
POOL_RECYCLE_SECONDS = 300
