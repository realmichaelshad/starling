# Security policy

## Reporting a problem

Please do not open a public issue for a security problem.

Report it privately through GitHub: open the repository's **Security** tab and choose **Report a
vulnerability**. That opens a private advisory only the maintainer can see.

Include what you can:

- what the problem is and what someone could do with it
- the steps or a small proof of concept that shows it
- the commit you tested

You can expect an answer within a week. If the report holds up, the fix is prepared in private, you
are credited in the advisory unless you would rather not be, and the advisory is published once the
fix is out.

## What is in scope

The code in this repository: the gateway, the services process, the supervisor, the engine, the
extension, the website and the deployment files.

There is no hosted Starling service run from this repository, so there is nothing live to test
against. Please test on an instance you run yourself. A problem in someone else's deployment of
Starling should go to whoever runs it; every instance serves its operator's contact at
`/.well-known/security.txt`.

## What is already known

[docs/SECURITY-REVIEW-2026-09-20.md](docs/SECURITY-REVIEW-2026-09-20.md) records a full review: what
was found and fixed, what was found sound, and the limitations that remain.
[docs/SECURITY-AND-PRIVACY.md](docs/SECURITY-AND-PRIVACY.md) describes the security model. A report
about something listed there as a known limitation is still welcome if you have found a way to make
it worse than described.

## Supported versions

Only the latest commit on `main` is supported.
