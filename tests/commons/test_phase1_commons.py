# ===========================================================================
# 9. Idempotency Cache (Hardened Mocks for Atomic Mechanics)
# ===========================================================================

class TestIdempotencyCache:

    def test_reserve_key_success(self):
        """Verify that a clean key can be successfully reserved with NX flags."""
        from fdq_commons.db.redis_client import IdempotencyCache
        cache = IdempotencyCache(namespace="activity", default_ttl=60)
        mock_redis = MagicMock()
        # Simulate Redis returning True for a successful SETNX operation
        mock_redis.set.return_value = True

        with patch("fdq_commons.db.redis_client.get_redis", return_value=mock_redis):
            success = cache.reserve_key("unique-key-123", lock_ttl=15)

        assert success is True
        mock_redis.set.assert_called_once_with(
            "idempotency:activity:unique-key-123", "PENDING", ex=15, nx=True
        )

    def test_reserve_key_collision_fails(self):
        """Verify that duplicate reservations fail when a key is already locked."""
        from fdq_commons.db.redis_client import IdempotencyCache
        cache = IdempotencyCache(namespace="audit", default_ttl=86400)
        mock_redis = MagicMock()
        # Simulate Redis returning None/False because the key already exists
        mock_redis.set.return_value = False

        with patch("fdq_commons.db.redis_client.get_redis", return_value=mock_redis):
            success = cache.reserve_key("duplicate-key-456")

        assert success is False

    def test_get_returns_pending_status(self):
        """Verify get handles active execution locks natively without trying to JSON decode."""
        from fdq_commons.db.redis_client import IdempotencyCache
        cache = IdempotencyCache(namespace="notification", default_ttl=60)
        mock_redis = MagicMock()
        mock_redis.get.return_value = "PENDING"

        with patch("fdq_commons.db.redis_client.get_redis", return_value=mock_redis):
            result = cache.get("active-lock-key")

        assert result == "PENDING"

    def test_set_and_get(self):
        from fdq_commons.db.redis_client import IdempotencyCache
        cache = IdempotencyCache(namespace="test", default_ttl=60)
        mock_redis = MagicMock()
        mock_redis.get.return_value = '{"log_id": "abc", "created_at": "2026-01-01"}'

        with patch("fdq_commons.db.redis_client.get_redis", return_value=mock_redis):
            result = cache.get("my-key")

        assert result == {"log_id": "abc", "created_at": "2026-01-01"}
        mock_redis.get.assert_called_once_with("idempotency:test:my-key")

    def test_get_returns_none_on_cache_miss(self):
        from fdq_commons.db.redis_client import IdempotencyCache
        cache = IdempotencyCache(namespace="test", default_ttl=60)
        mock_redis = MagicMock()
        mock_redis.get.return_value = None

        with patch("fdq_commons.db.redis_client.get_redis", return_value=mock_redis):
            result = cache.get("missing-key")

        assert result is None

    def test_set_uses_correct_ttl(self):
        from fdq_commons.db.redis_client import IdempotencyCache
        cache = IdempotencyCache(namespace="audit", default_ttl=86400)
        mock_redis = MagicMock()

        with patch("fdq_commons.db.redis_client.get_redis", return_value=mock_redis):
            cache.set("my-key", {"event_id": "xyz"})

        mock_redis.set.assert_called_once()
        call_kwargs = mock_redis.set.call_args.kwargs
        assert call_kwargs.get("ex") == 86400

    def test_custom_ttl_overrides_default(self):
        from fdq_commons.db.redis_client import IdempotencyCache
        cache = IdempotencyCache(namespace="activity", default_ttl=60)
        mock_redis = MagicMock()

        with patch("fdq_commons.db.redis_client.get_redis", return_value=mock_redis):
            cache.set("my-key", {"log_id": "zzz"}, ttl=30)

        call_kwargs = mock_redis.set.call_args.kwargs
        assert call_kwargs.get("ex") == 30

    def test_namespace_prevents_collision(self):
        from fdq_commons.db.redis_client import IdempotencyCache
        cache_a = IdempotencyCache(namespace="activity", default_ttl=60)
        cache_b = IdempotencyCache(namespace="audit", default_ttl=86400)
        mock_redis = MagicMock()
        mock_redis.get.return_value = None

        with patch("fdq_commons.db.redis_client.get_redis", return_value=mock_redis):
            cache_a.get("same-key")
            cache_b.get("same-key")

        # Extract arguments safely from call_args paths
        args_a = mock_redis.get.call_args_list[0].args[0]
        args_b = mock_redis.get.call_args_list[1].args[0]
        assert "activity:same-key" in args_a
        assert "audit:same-key" in args_b