"""Which selections are ours. From `GET /api/v1/slots`: columns 5-8 of every row on `vmc1`."""

DEVICE_ID = "vmc1"

SLOTS: tuple[str, ...] = tuple(f"{row}{col}" for row in range(1, 6) for col in range(5, 9))
