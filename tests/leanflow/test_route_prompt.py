from leanflow_cli.native.route_prompt import active_route_obligation_block


def test_negate_route_obligation_blocks_prior_constructive_route():
    block = active_route_obligation_block(
        {
            "route_action": "negate",
            "reason": "test the target for a counterexample",
        }
    )

    assert "[LEANFLOW ACTIVE ROUTE OBLIGATION]" in block
    assert "assigned strategy: `negate`" in block
    assert "counterexample or consistency audit" in block
    assert "do not resume the prior constructive proof" in block
    assert "kernel-verified proof of the assigned target" in block


def test_generic_queue_route_has_no_strategy_obligation():
    assert active_route_obligation_block({"route_action": "queue-worker"}) == ""


def test_decompose_route_obligation_requires_structural_evidence():
    block = active_route_obligation_block({"route_action": "decompose"})

    assert "checked helper decomposition" in block
    assert "unchanged monolithic attempt" in block
