# Integrate upstream changes into NDS

[한국어](upstream-sync.md) · **English** · [NDS documentation](README.en.md)

Use a dedicated integration branch and review the upstream base against `nds/dev`. Keep environment-specific behavior represented in the NDS paths and verify overlapping deployment, identity, governance, and client changes.

1. Fetch the intended upstream and NDS references and identify the merge base.
2. Start from the agreed target branch. Review the incoming change and how it interacts with NDS. Use Git rerere only with conflict resolutions you can verify.
3. Resolve conflicts against both sides' current behavior. Check shared components, schema and migration order, and the deployment renderers.
4. Run the affected suites, review the complete diff, and follow the repository's review and merge flow.
5. Confirm the resulting commit and any follow-up compatibility notes before deployment.

For exact reference names and the conflict catalog, use the [original synchronization procedure](upstream-sync.md). The [transition records](upstream-diff/README.en.md) describe the earlier comparison snapshot.
