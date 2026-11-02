# Keep live-provider tests separate from fixture-driven E2E tests

Tests that connect to the existing AmazingData provider are explicitly run outside regular CI and fail visibly when connection, permissions, or data are unavailable. Regular CI runs end-to-end workflows using reviewed, versioned captures of real market data for `563360`; this keeps provider access verifiable without making CI depend on external credentials or changing market conditions.
