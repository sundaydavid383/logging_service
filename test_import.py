# test_import.py

print("1")

from fdq_commons.db.redis_client import get_redis

print("2")

r = get_redis()

print("3")

print(r.ping())

print("4")