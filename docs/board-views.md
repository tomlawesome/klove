# Board and saved views

Delivery tracking lives on the GitLab issue board
[Klove Delivery Roadmap](https://gitlab.tomlawson.io/ai/klove/-/boards/2):

    Open -> status: needs-decision -> status: blocked -> status: ready
         -> status: in-progress -> Closed

`Open` is the backlog (no `status:` label). `Closed` is done.

The board replaces the GitHub Projects v2 board of the same name, which is
closed. Every field that board carried is now ordinary issue state: `status:`,
`priority:`, `risk:` and `area:` labels, plus the milestone. Its `Slice` field
duplicated the milestone and was dropped.

## Saved views

GitLab Community Edition gives a project one board, so the GitHub board's five
saved views become issue-list filters instead. The list ANDs labels together —
this instance ignores the `or[label_name][]` parameter — so a view that was one
OR filter is two links here.

All links are relative to `https://gitlab.tomlawson.io/ai/klove/-/issues`.

### Current wave

Open work at the top two priorities.

- `?state=opened&label_name[]=priority%3A%20p0`
- `?state=opened&label_name[]=priority%3A%20p1`

### Luna-ready

Open work marked safe to run in parallel.

- `?state=opened&label_name[]=parallel%3A%20candidate`

### Blocked and decisions

The board's first two columns already are this view. As links:

- `?state=opened&label_name[]=status%3A%20blocked`
- `?state=opened&label_name[]=status%3A%20needs-decision`

### Safety and control

Work on a safety or correctness boundary.

- `?state=opened&label_name[]=risk%3A%20safety-critical` — actuation safety
- `?state=opened&label_name[]=risk%3A%20high` — security or durable correctness

The GitHub board also graded issues Medium, Low and None. Those grades stopped
being recorded after #40 and are not reproduced; absence of a `risk:` label
means the issue is not on a safety or correctness boundary.

### Release train

Not reproduced. On GitHub this filtered the `Release / CI` delivery lane, which
held one issue (#3). No GitLab label corresponds to it, and no open issue
carries `release` or `ci: acceptance`.
