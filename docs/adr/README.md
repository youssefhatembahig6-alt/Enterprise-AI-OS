# Architecture decision records (repository)

ADR numbers 0001–0020 belong to the decision table in section 13 of the
[project specification](https://app.notion.com/p/Enterprise-AI-OS-Project-Documentation-41e095eca45681919bddcf0227a8699f). That table marks some rows **Proposed**; this
repository does not change their status, and nothing here records a team
meeting. Repository ADRs start at 0101 so they never collide with the source list.

| ADR | Title | Status | Relates to source |
| --- | --- | --- | --- |
| [0101](0101-identity-and-session.md) | Identity and session contract | Accepted for M0 (pending planning review) | §8 JWT authentication, FR-01, NFR-05 |
| [0102](0102-tenant-boundary.md) | Tenant boundary in PostgreSQL | Accepted for M0 (pending planning review) | §8 multi-tenant isolation, ADR 0003, NFR-01 |
| [0103](0103-authorization-reference-policy.md) | Reference authorization policy | Accepted for M0 (pending planning review) | §8 five layers, ADR 0004, ADR 0012 (Proposed) |
| [0104](0104-retrieval-eligible-ids-design.md) | Eligible-ID retrieval design (design only) | Proposed | ADR 0004, ADR 0012 (Proposed), FR-02 |
| [0105](0105-runtime-and-compose.md) | Runtime, pinned versions and Compose | Accepted for M0 (pending planning review) | §7, ADR 0007, 0008, 0013, NFR-03/04 |
