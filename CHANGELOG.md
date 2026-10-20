# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed

- A change to the worker's consumer settings (`ack_wait`, `max_deliver`) now
  reaches the existing JetStream consumers when the worker restarts. nats-py
  applies them only when it creates a durable, so until now a new value was
  silently ignored by every deployment that already had the consumer. The
  worker logs the old and new values.
- A rendered-PDF result the worker cannot use (document-generation gave up
  without calling the failure permanent, a success without a PDF, or a crash
  while attaching it) is now retried 5 times, then parked on
  `optimce.billing.dlq.docgen_result` and acknowledged. It used to be
  redelivered every 30 seconds forever. The invoice is left as it is, so its PDF
  can be requested again.
