# Exported image context (review draft)

The `Dockerfile` is code preparation. No image build, pull, registry publication
or deployment is implied. Approve exact base/platform, source, wheel, dependency
locks, toolchain and context first. The recipe has no base defaults and does not
select a production provider. Runtime identity, secrets, source scope, ingress,
egress and enforced cost limits belong to a separate service configuration review.

The builder must receive an exported directory containing only:

| Path | Input |
| --- | --- |
| `Dockerfile`, `.dockerignore` | Exact bytes from this reviewed source. |
| `image-context.json` | Exact `host-image-context/1` manifest. |
| `host/**` | Committed public app and required contracts/configuration; no legacy planner/router implementation. |
| `wheels/*.whl` | Hashed dependency/provider wheels, no editable installs or shadow app/bridge packages. |
| `locks/runtime.lock` | Complete version/hash locked, wheel-only install input; every included wheel hash must appear. |

Run the offline validator against that exported directory before any separately
approved build:

```sh
python scripts/check_image_context.py /absolute/path/to/exported-context
```

The context manifest has exactly `schema`, `base_image`, `platform`, `host`,
`wheel`, `locks`, `toolchain`, `files`. Use `schema=host-image-context/1`.
`base_image` is registry/name`@sha256:` plus 64 lowercase hex with no mutable tag;
`platform` is explicitly `linux/amd64` or `linux/arm64`, backed by a reviewed lock.
`host` records 40-character `commit`/`tree` and `source_sha256`. `wheel` records
relative `path`, `sha256`, `source_commit`, `source_tree`, `input_sha256`.
`locks` binds `runtime`, `build`, `host` SHA-256 values. `toolchain` records
`python`, `pip`, `builder`. `files` maps every actual context file except the
manifest itself to its SHA-256. Do not put repository credentials, private source
locators, local usernames, cases or runtime secrets in this generic manifest.
Use a separate private review packet for any optional provider-specific identity.

The checker verifies exact recipe/ignore bytes, canonical allowlisted paths and
file hashes, rejects all symlinks and forbidden artifact paths, credential-shaped
content, legacy imports, wheel path escape/editable/shadow packages and remote or
editable requirements. Every supported wheel `.data` scheme (purelib, platlib,
scripts, data and headers) is normalized to its effective install destination
before sensitive-path and app-shadow checks; unknown schemes and colliding
effective destinations are refused. Python import checks also inspect relocated
code. The recipe installs only local hashed wheels with no
index/cache/compilation and runs as UID/GID 65532. The optional compatibility
loader can remain to return unavailable; `app/agent/planner.py` and `router.py`
are excluded. This does not retire public legacy source or change normal runtime.

Context consistency is not proof of a trusted export, vulnerability safety,
installed origins, live service readiness or authorization. Review the committed
source identities and build toolchain independently. After approved build inputs,
inspect/test the actual image, layers, installed files and immutable image digest,
then review its provenance/configuration before registry/deployment authorization.
No recipe invocation or cloud command is provided as an implicitly approved step.
