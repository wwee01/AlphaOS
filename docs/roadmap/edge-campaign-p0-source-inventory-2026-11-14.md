# P0.1 exact production-source inventory

Base: `de1bfea27e9068da3fc7cd6419580e8767cad7d9`. Each file below is retained and committed byte-for-byte from the running checkout. This records existing code, not a deployment or blanket approval of its behavior. No `.env`, database, marker, log, credential or historical outcome is imported. Known observer findings remain; CK freezes repairs and P0.2 detaches its effect on heartbeat status. No running file is removed in P0.1.

| File | Disposition | SHA256 |
|---|---|---|
| `alphaos/data/providers/alpaca_data.py` | Retain; commit exact bytes for Claude audit | `9713c4f34d5f0f44a7d7d388ce9d6d59aec2894cf59b46a86419b20dc221dd33` |
| `alphaos/scheduler/worker.py` | Retain; commit exact bytes for Claude audit | `976f89735e0cf6cd9bd215c76d87e9f6d2c0c8ae59b2e31de304eb3712f4b1de` |
| `alphaos/monitoring/__init__.py` | Retain; commit exact bytes for Claude audit | `5a8cca9acfb65c32f1f5e778969d288a9759e89277cd35bd2c4365d83a3b4833` |
| `alphaos/monitoring/__main__.py` | Retain; commit exact bytes for Claude audit | `3f3e33d86b8fcef4a0b918ccf3806d350aa4b0d3f3cce4b5aa8f055243a4856c` |
| `alphaos/monitoring/activation.py` | Retain; commit exact bytes for Claude audit | `e2514a4cae9ba3e709c41d3abfa7e6cdde2d14cca1249e4f03a07e87b97eec22` |
| `alphaos/monitoring/broker_reads.py` | Retain; commit exact bytes for Claude audit | `b644fe9d6892b62021686345ceaed20a1e5e2c5ec3f4fe15a4e455962583d3a7` |
| `alphaos/monitoring/heartbeat.py` | Retain; commit exact bytes for Claude audit | `815ce5581bed2bcbbf59381a23c1aa88b9c60fb454fc91fd492521a73f9366e8` |
| `alphaos/monitoring/incidents.py` | Retain; commit exact bytes for Claude audit | `946911a8de2a90edf54e481d3900a0aae595a2d1085247b3ff54851d0302dffd` |
| `alphaos/monitoring/observer.py` | Retain; commit exact bytes for Claude audit | `15d0c7e56bf106f7dfc786951f62223521076918fbae583643cdd474235648c4` |
| `alphaos/monitoring/poll.py` | Retain; commit exact bytes for Claude audit | `aaf8e4a08a3ae3dae9130cf950da0fb01c8edd60036420cb469e764621d7bed4` |
| `alphaos/research/data_freshness_trial.py` | Retain; commit exact bytes for Claude audit | `5cac38340a53bfba2b57b76fa7f92f511e492224534ed5d2aa1ee456598829a4` |
| `alphaos/research/swing_audit.py` | Retain; commit exact bytes for Claude audit | `af8b7ec201008ebfd5f94bd685da6c1e306c1309f9ed8ba888a676e98e8bedd4` |
| `alphaos/research/swing_audit_worker.py` | Retain; commit exact bytes for Claude audit | `c0e6c18083d734c560d2bbc34de68cc37dcff0de330fdeca6515921daa40adde` |
| `alphaos/research/swing_data_policy.py` | Retain; commit exact bytes for Claude audit | `cd7d0190953a9d33080eba392c58e262cb064f5c23f702c861d120615422b146` |
| `alphaos/research/swing_session_evidence.py` | Retain; commit exact bytes for Claude audit | `974d36a1fc7dd5b63235774119ecad9005d3632ed3404ff42cb7c6dacf79e105` |
| `deploy/com.ck.alphaos.heartbeat-observer.plist` | Retain; commit exact bytes for Claude audit | `aae9d67e1f9641d6ebf29256f589cf997eef1130ea7d1db6202660365779446a` |
| `tests/fixtures/swing_freshness_forward_20261015_16.json.gz` | Retain; commit exact bytes for Claude audit | `ae61859401e53a05b721a3cdb16ffd0b43741b630134a65b6bd5f99a11d1fd2c` |
| `tests/test_data_freshness_trial.py` | Retain; commit exact bytes for Claude audit | `c105fba945df05be4c6652b852e76c54a27dc863b0e7d143220511e6ab177ee2` |
| `tests/test_observer_activation.py` | Retain; commit exact bytes for Claude audit | `1d42f609e12d7c002fc0b93658a0985b223bef58e3c4f2cc6f0b0b03c93c17af` |
| `tests/test_observer_heartbeat.py` | Retain; commit exact bytes for Claude audit | `b27e57b7281cc3707f87fe8a225a5c7fc5f457a825297d058cec3a14ea36d29c` |
| `tests/test_paper_observer.py` | Retain; commit exact bytes for Claude audit | `1a499df77ed4ba486b7329740de7cbc88d25e0041f25dbd585e4bf9758a44160` |
| `tests/test_swing_audit.py` | Retain; commit exact bytes for Claude audit | `3d6d9d808d39edfcb57ac7cdb7cefdd129c0d96770f68e564ef2766c1796d1cd` |
| `tests/test_swing_data_policy.py` | Retain; commit exact bytes for Claude audit | `3001ceac6a3265093a2e21797a6916d3b6c82dd263f3536c9f435f8acc746854` |

Follow-up changes are separate commits. Production remains untouched until Claude audits the exact SHA and CK gives the merge instruction.
