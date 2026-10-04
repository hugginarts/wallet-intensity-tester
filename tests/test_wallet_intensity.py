import unittest
from unittest.mock import patch

import wallet_intensity_v2 as wi


WALLET = "11111111111111111111111111111111"
MINT = "TokenMint1111111111111111111111111111111"
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"


def tx(native_pre, native_post, pre_amount, post_amount, block_time, slot):
    pre_tokens = [] if pre_amount is None else [{
        "accountIndex": 1, "mint": MINT, "owner": WALLET,
        "uiTokenAmount": {"amount": str(pre_amount), "decimals": 2},
    }]
    post_tokens = [] if post_amount is None else [{
        "accountIndex": 1, "mint": MINT, "owner": WALLET,
        "uiTokenAmount": {"amount": str(post_amount), "decimals": 2},
    }]
    return {
        "slot": slot,
        "blockTime": block_time,
        "transaction": {"message": {"accountKeys": [WALLET, "TokenAccount111111111111111111111111111"]}},
        "meta": {
            "err": None,
            "preBalances": [native_pre, 0],
            "postBalances": [native_post, 0],
            "preTokenBalances": pre_tokens,
            "postTokenBalances": post_tokens,
        },
    }


class ParseTransactionTests(unittest.TestCase):
    def test_rpc_request_accepts_solana_v1_transactions(self):
        with patch.object(wi, "rpc", return_value={"version": 1}) as mocked:
            wi.fetch_transaction("https://rpc.example", "signature", None)
        config = mocked.call_args.args[2][1]
        self.assertEqual(config["maxSupportedTransactionVersion"], 1)
        self.assertEqual(config["encoding"], "jsonParsed")

    def test_swap_includes_network_fee_but_removes_token_account_rent(self):
        # 0.1 SOL swap + 0.00203928 account rent + 0.000005 network fee.
        buy = tx(5_000_000_000, 4_897_955_720, None, 1_000, 100, 1)
        kind, data = wi.parse_tx(buy, WALLET)
        self.assertEqual(kind, "swap")
        self.assertEqual(data["q"], 10)
        self.assertAlmostEqual(data["sol"], -0.100005, places=8)

    def test_sale_recognizes_returned_account_rent_and_network_fee(self):
        # 0.1 SOL proceeds + rent refund - 0.000005 network fee.
        sale = tx(4_897_955_720, 4_999_990_000, 1_000, None, 130, 2)
        kind, data = wi.parse_tx(sale, WALLET)
        self.assertEqual(kind, "swap")
        self.assertEqual(data["q"], -10)
        self.assertAlmostEqual(data["sol"], 0.099995, places=8)

    def test_failed_transaction_is_not_counted_as_a_swap(self):
        failed = tx(5_000_000_000, 4_900_000_000, None, 1_000, 100, 1)
        failed["meta"]["err"] = {"InstructionError": [1, "failed"]}
        self.assertEqual(wi.parse_tx(failed, WALLET), ("failed", None))

    def test_usdc_quoted_swap_is_parsed_in_usd(self):
        buy = tx(5_000_000_000, 4_999_995_000, None, 1_000, 100, 1)
        buy["meta"]["postTokenBalances"].append({
            "accountIndex": 2, "mint": USDC, "owner": WALLET,
            "uiTokenAmount": {"amount": "0", "decimals": 6},
        })
        buy["meta"]["preTokenBalances"] = [{
            "accountIndex": 2, "mint": USDC, "owner": WALLET,
            "uiTokenAmount": {"amount": "5000000", "decimals": 6},
        }]
        kind, data = wi.parse_tx(buy, WALLET)
        self.assertEqual(kind, "swap")
        self.assertEqual(data["quote"], "USD")
        self.assertEqual(data["sol"], -5.0)
        self.assertEqual(data["q"], 10)

    def test_usdc_round_trip_reports_usd_without_mixing_sol(self):
        trades = [
            {"mint": MINT, "q": 10, "sol": -5, "quote": "USD", "t": 100, "slot": 1},
            {"mint": MINT, "q": -10, "sol": 7, "quote": "USD", "t": 160, "slot": 2},
        ]
        report = wi.build_report({"wallet": WALLET, "t_start": 100, "t_end": 200,
                                  "reviewed": 2, "failed_sigs": 0, "diag": {"swap": 2}}, trades,
                                 size=10, slip=0, fee=0)
        self.assertEqual(report["unit"], "USD")
        self.assertEqual(report["n_closed"], 1)
        self.assertAlmostEqual(report["net"], 2.0)


class PositionAndReportTests(unittest.TestCase):
    def test_zero_closed_positions_are_not_labeled_as_zero_profit(self):
        report = wi.build_report({"wallet": WALLET, "t_start": 100, "t_end": 200,
                                  "reviewed": 50, "failed_sigs": 0, "diag": {}}, [])
        self.assertEqual(report["n_closed"], 0)
        self.assertEqual(report["title"], "SIN OPERACIONES COMPATIBLES")
        self.assertEqual(report["net"], 0)

    def test_fifo_position_closes_and_counts_orphan_sale(self):
        trades = [
            {"mint": MINT, "q": 10, "sol": -0.1, "t": 100, "slot": 1},
            {"mint": MINT, "q": 10, "sol": -0.2, "t": 110, "slot": 2},
            {"mint": MINT, "q": -10, "sol": 0.15, "t": 130, "slot": 3},
            {"mint": MINT, "q": -10, "sol": 0.3, "t": 150, "slot": 4},
            {"mint": "OtherMint11111111111111111111111111111", "q": -2,
             "sol": 0.05, "t": 160, "slot": 5},
        ]
        closed, open_positions, orphans = wi.analyze(trades, 200)
        self.assertEqual(len(closed), 1)
        self.assertEqual(open_positions, [])
        self.assertEqual(orphans, 1)
        self.assertAlmostEqual(closed[0]["cost"], 0.3)
        self.assertAlmostEqual(closed[0]["proceeds"], 0.45)
        self.assertAlmostEqual(closed[0]["pnl"], 0.15)

    def test_copy_simulation_subtracts_entry_exit_slippage_and_each_leg_fee(self):
        closed = [{"cost": 0.1, "proceeds": 0.2, "legs": 2}]
        pnl = wi.copy_pnls(closed, size=0.1, s_in=0, s_out=0, fee=0.003)
        self.assertAlmostEqual(pnl[0], 0.094)

    def test_positive_historical_sample_is_labeled_as_paper_candidate(self):
        trades = []
        for i in range(30):
            t = 1_000 + i * 60
            trades.append({"mint": f"Mint{i}", "q": 100, "sol": -0.1, "t": t, "slot": i * 2})
            trades.append({"mint": f"Mint{i}", "q": -100, "sol": 0.2, "t": t + 30, "slot": i * 2 + 1})
        report = wi.build_report({"wallet": WALLET, "t_start": 1_000, "t_end": 60_000,
                                  "reviewed": 100, "failed_sigs": 0, "diag": {"swap": 60}}, trades)
        self.assertEqual(report["verdict"], "yes")
        self.assertIn("PAPER", report["title"])
        self.assertNotIn("ES RENTABLE", report["title"])


if __name__ == "__main__":
    unittest.main()
