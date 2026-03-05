"""Tests for LLMPlayer._analyze_draws"""
from llm import LLMPlayer

analyze = LLMPlayer._analyze_draws


# --- Flush ---

def test_flush_one_hole_card():
    # Qd contributes to 5 diamonds
    result = analyze(["Ts", "Qd"], ["3d", "9d", "Jd", "4d", "7c"])
    assert result.get("flush") is True
    assert "flush_draw" not in result

def test_flush_two_hole_cards():
    result = analyze(["Ah", "Kh"], ["2h", "5h", "9h"])
    assert result.get("flush") is True

def test_flush_draw_two_hole_cards():
    result = analyze(["Ad", "Kd"], ["3d", "9d", "7c"])
    assert result.get("flush_draw") is True
    assert "flush" not in result

def test_flush_draw_one_hole_card():
    result = analyze(["As", "Kd"], ["3d", "9d", "Jd", "7c"])
    assert result.get("flush_draw") is True

def test_no_flush_no_matching_suit():
    # Board has 3 spades but hole cards have 0 spades
    result = analyze(["Ah", "Kd"], ["3s", "9s", "Js", "4c", "7c"])
    assert "flush" not in result
    assert "flush_draw" not in result

def test_no_flush_draw_only_two_of_suit():
    result = analyze(["Ad", "Kc"], ["3d", "9s", "Jh"])
    assert "flush" not in result
    assert "flush_draw" not in result

def test_board_four_flush_but_no_hole_card():
    # 4 hearts on board but no hearts in hand
    result = analyze(["As", "Kc"], ["2h", "5h", "9h", "Jh", "3d"])
    assert "flush" not in result
    assert "flush_draw" not in result


# --- Straight ---

def test_straight_made():
    # 8-9-T-J-Q straight
    result = analyze(["Ts", "Jd"], ["8c", "9h", "Qd"])
    assert result.get("straight") is True

def test_oesd():
    # J-T-9-8 open ended
    result = analyze(["Jc", "Tc"], ["9d", "8s", "2c"])
    assert result.get("oesd") is True
    assert "straight" not in result

def test_gutshot():
    # J-T-_-8-7 gutshot (needs 9)
    result = analyze(["Jc", "Tc"], ["8d", "7s", "2c"])
    assert result.get("gutshot") is True
    assert "oesd" not in result
    assert "straight" not in result

def test_wheel_straight():
    # A-2-3-4-5
    result = analyze(["Ah", "2d"], ["3c", "4s", "5h"])
    assert result.get("straight") is True

def test_wheel_gutshot():
    # A-2-_-4-5 (needs 3)
    result = analyze(["Ah", "2d"], ["4c", "5s", "9h"])
    assert result.get("gutshot") is True

def test_no_straight_draw():
    result = analyze(["2h", "7d"], ["Ks", "Jc", "4h"])
    assert "straight" not in result
    assert "oesd" not in result
    assert "gutshot" not in result

def test_broadway_straight():
    # A-K-Q-J-T
    result = analyze(["Ah", "Kd"], ["Qs", "Jc", "Th"])
    assert result.get("straight") is True


# --- Pairs ---

def test_top_pair():
    result = analyze(["Ah", "7d"], ["As", "Jc", "4h"])
    assert result.get("top_pair") is True

def test_mid_pair():
    result = analyze(["Jh", "7d"], ["As", "Jc", "4h"])
    assert result.get("mid_pair") is True

def test_bottom_pair():
    result = analyze(["4s", "7d"], ["As", "Jc", "4h"])
    assert result.get("bottom_pair") is True

def test_two_pair():
    result = analyze(["As", "Jd"], ["Ah", "Jc", "4h"])
    assert result.get("two_pair") is True

def test_trips():
    result = analyze(["As", "7d"], ["Ah", "Ac", "4h"])
    assert result.get("trips") is True

def test_full_house():
    result = analyze(["As", "4d"], ["Ah", "Ac", "4h"])
    assert result.get("full_house") is True

def test_quads():
    result = analyze(["As", "Ad"], ["Ah", "Ac", "4h"])
    assert result.get("quads") is True

def test_pocket_pair_no_board_match():
    # KK on 3-7-J board — overpair
    result = analyze(["Ks", "Kd"], ["3h", "7c", "Jh"])
    assert result.get("overpair") is True
    assert "two_pair" not in result
    assert "top_pair" not in result
    assert result.get("overcards") == 2

def test_pocket_pair_underpair():
    # 33 on K-J-7 board — pocket pair (not overpair)
    result = analyze(["3s", "3d"], ["Kh", "Jc", "7h"])
    assert result.get("pocket_pair") is True
    assert "overpair" not in result

def test_pocket_pair_hits_board_set():
    # KK on K-7-3 board — trips
    result = analyze(["Ks", "Kd"], ["Kh", "7c", "3h"])
    assert result.get("trips") is True

def test_pocket_pair_plus_board_pair():
    # KK on 7-7-3 board — two pair (KK + 77)
    result = analyze(["Ks", "Kd"], ["7h", "7c", "3h"])
    assert result.get("two_pair") is True

def test_no_pair():
    result = analyze(["2h", "7d"], ["Ks", "Jc", "4h"])
    assert "top_pair" not in result
    assert "mid_pair" not in result
    assert "bottom_pair" not in result
    assert "two_pair" not in result
    assert "trips" not in result


# --- Overcards ---

def test_two_overcards():
    result = analyze(["Ah", "Kd"], ["3s", "9c", "Jh"])
    assert result.get("overcards") == 2

def test_one_overcard():
    result = analyze(["Ah", "3d"], ["9s", "Jc", "4h"])
    assert result.get("overcards") == 1

def test_no_overcards():
    result = analyze(["3h", "2d"], ["9s", "Jc", "4h"])
    assert result.get("overcards") is None or result.get("overcards", 0) == 0


# --- Empty/preflop ---

def test_preflop_no_board():
    result = analyze(["Ah", "Kd"], [])
    assert result == {}

def test_no_hole_cards():
    result = analyze([], ["3s", "9c", "Jh"])
    assert result == {}


# --- Combo draws ---

def test_flush_draw_plus_oesd():
    # Jd Td on 9d 8d 2c — flush draw + OESD
    result = analyze(["Jd", "Td"], ["9d", "8d", "2c"])
    assert result.get("flush_draw") is True
    assert result.get("oesd") is True

def test_flush_plus_straight():
    result = analyze(["Jd", "Td"], ["9d", "8d", "Qd"])
    assert result.get("flush") is True
    assert result.get("straight") is True

def test_top_pair_plus_flush_draw():
    result = analyze(["Ad", "Kd"], ["As", "3d", "7d"])
    assert result.get("top_pair") is True
    assert result.get("flush_draw") is True


# --- Edge cases ---

def test_ten_as_T():
    # Make sure T = 10 works
    result = analyze(["Th", "Td"], ["3s", "9c", "Jh"])
    assert result.get("overcards") is None or result.get("overcards", 0) == 0

def test_paired_board_no_hole_match():
    # Board has pair of 6s but we don't match
    result = analyze(["8s", "4c"], ["7s", "Js", "4d", "6d", "6c"])
    assert result.get("bottom_pair") is True
    assert "trips" not in result
    assert "two_pair" not in result


if __name__ == "__main__":
    import sys
    passed = 0
    failed = 0
    tests = [(name, func) for name, func in globals().items() if name.startswith("test_") and callable(func)]
    for name, func in tests:
        try:
            func()
            passed += 1
            print(f"  PASS  {name}")
        except AssertionError as e:
            failed += 1
            # Show actual value for debugging
            print(f"  FAIL  {name}: {e}")
        except Exception as e:
            failed += 1
            print(f"  ERROR {name}: {e}")
    print(f"\n{passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)
