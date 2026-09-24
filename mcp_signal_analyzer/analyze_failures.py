#!/usr/bin/env python3
"""
Analyze why signals failed - which indicators were wrong
"""

import re
from datetime import datetime, timedelta

# Read signal queue to get original signal data
signals_to_check = [
    # (symbol, direction, time, result)
    ("COOKIE", "LONG", "2026-08-06 19:11", "TP"),  # WIN
    ("BEL", "LONG", "2026-08-06 19:34", "SL"),     # LOSS
    ("KMNO", "LONG", "2026-08-07 05:17", "SL"),    # LOSS
    ("CARV", "LONG", "2026-08-07 05:43", "SL"),    # LOSS
    ("PUMPFUN", "SHORT", "2026-08-07 05:47", "SL"), # LOSS
    ("BSB", "LONG", "2026-08-07 06:04", "TP"),     # WIN
    ("BIGTIME", "LONG", "2026-08-07 06:43", "SL"), # LOSS
    ("FARTCOIN", "LONG", "2026-08-07 06:59", "SL"), # LOSS
    ("ZORA", "LONG", "2026-08-07 07:30", "SL"),    # LOSS
    ("SKL", "SHORT", "2026-08-07 08:29", "SL"),    # LOSS
    ("ONDO", "LONG", "2026-08-07 08:33", "SL"),    # LOSS
]

def parse_scanner_log():
    """Parse scanner.log to extract signal details"""
    log_file = "/home/max/o_p/dex_scanner/logs/scanner.log"
    
    signals = {}
    
    try:
        with open(log_file, 'r', encoding='utf-8') as f:
            content = f.read()
            
        # Find SIGNAL_DECISION blocks
        pattern = r'SIGNAL_DECISION.*?Symbol: (\w+).*?Direction: (\w+).*?Score: (\d+)/15.*?RSI: ([\d.]+).*?Phase: (\w+).*?VW-MACD: ([\w-]+).*?Lorentzian: ([-+]\d+)\((\w+)\)'
        
        matches = re.finditer(pattern, content, re.DOTALL)
        
        for match in matches:
            symbol = match.group(1)
            direction = match.group(2)
            score = int(match.group(3))
            rsi = float(match.group(4))
            phase = match.group(5)
            macd = match.group(6)
            lorentzian_val = int(match.group(7))
            lorentzian_dir = match.group(8)
            
            key = f"{symbol}_{direction}"
            
            if key not in signals or score > signals[key]['score']:
                signals[key] = {
                    'symbol': symbol,
                    'direction': direction,
                    'score': score,
                    'rsi': rsi,
                    'phase': phase,
                    'macd': macd,
                    'lorentzian_val': lorentzian_val,
                    'lorentzian_dir': lorentzian_dir,
                }
    
    except Exception as e:
        print(f"Error reading scanner.log: {e}")
    
    return signals

def analyze_indicators():
    """Analyze which indicators were correct/wrong"""
    
    scanner_signals = parse_scanner_log()
    
    print("="*80)
    print("АНАЛИЗ ИНДИКАТОРОВ")
    print("="*80)
    print()
    
    # Track indicator accuracy
    indicator_stats = {
        'rsi': {'correct': 0, 'wrong': 0},
        'phase': {'correct': 0, 'wrong': 0},
        'macd': {'correct': 0, 'wrong': 0},
        'lorentzian': {'correct': 0, 'wrong': 0},
    }
    
    for symbol, direction, time_str, result in signals_to_check:
        key = f"{symbol}_{direction}"
        
        print(f"\n{'='*80}")
        print(f"📊 {symbol} {direction} - {result}")
        print(f"⏰ {time_str}")
        
        if key not in scanner_signals:
            print("⚠️ Signal not found in scanner.log")
            continue
        
        sig = scanner_signals[key]
        
        print(f"\n📈 Score: {sig['score']}/15")
        print(f"   RSI: {sig['rsi']}")
        print(f"   Phase: {sig['phase']}")
        print(f"   VW-MACD: {sig['macd']}")
        print(f"   Lorentzian: {sig['lorentzian_val']} ({sig['lorentzian_dir']})")
        
        # Check each indicator
        is_win = (result == "TP")
        
        # RSI analysis
        if direction == "LONG":
            rsi_bullish = sig['rsi'] < 40  # Oversold for long
        else:
            rsi_bullish = sig['rsi'] > 60  # Overbought for short
        
        rsi_correct = (rsi_bullish and direction == "LONG" and is_win) or \
                     (not rsi_bullish and direction == "LONG" and not is_win) or \
                     (rsi_bullish and direction == "SHORT" and not is_win) or \
                     (not rsi_bullish and direction == "SHORT" and is_win)
        
        # Phase analysis
        phase_bullish = "EXPANSION" in sig['phase'] or "ACCUMULATION" in sig['phase']
        phase_correct = (phase_bullish and direction == "LONG" and is_win) or \
                       (not phase_bullish and direction == "LONG" and not is_win) or \
                       (phase_bullish and direction == "SHORT" and not is_win) or \
                       (not phase_bullish and direction == "SHORT" and is_win)
        
        # MACD analysis
        macd_bullish = "bullish" in sig['macd'].lower()
        macd_correct = (macd_bullish and direction == "LONG" and is_win) or \
                      (not macd_bullish and direction == "LONG" and not is_win) or \
                      (macd_bullish and direction == "SHORT" and not is_win) or \
                      (not macd_bullish and direction == "SHORT" and is_win)
        
        # Lorentzian analysis
        lorentzian_bullish = sig['lorentzian_dir'] == "LONG"
        lorentzian_correct = (lorentzian_bullish and direction == "LONG" and is_win) or \
                            (not lorentzian_bullish and direction == "LONG" and not is_win) or \
                            (lorentzian_bullish and direction == "SHORT" and not is_win) or \
                            (not lorentzian_bullish and direction == "SHORT" and is_win)
        
        print(f"\n✅/❌ Индикаторы:")
        print(f"   RSI: {'✅ correct' if rsi_correct else '❌ wrong'}")
        print(f"   Phase: {'✅ correct' if phase_correct else '❌ wrong'}")
        print(f"   MACD: {'✅ correct' if macd_correct else '❌ wrong'}")
        print(f"   Lorentzian: {'✅ correct' if lorentzian_correct else '❌ wrong'}")
        
        # Update stats
        if rsi_correct:
            indicator_stats['rsi']['correct'] += 1
        else:
            indicator_stats['rsi']['wrong'] += 1
        
        if phase_correct:
            indicator_stats['phase']['correct'] += 1
        else:
            indicator_stats['phase']['wrong'] += 1
        
        if macd_correct:
            indicator_stats['macd']['correct'] += 1
        else:
            indicator_stats['macd']['wrong'] += 1
        
        if lorentzian_correct:
            indicator_stats['lorentzian']['correct'] += 1
        else:
            indicator_stats['lorentzian']['wrong'] += 1
    
    # Print summary
    print(f"\n\n{'='*80}")
    print("📊 ИТОГИ: Точность индикаторов")
    print("="*80)
    
    for indicator, stats in sorted(indicator_stats.items(), 
                                   key=lambda x: x[1]['correct']/(x[1]['correct']+x[1]['wrong']) if x[1]['correct']+x[1]['wrong'] > 0 else 0,
                                   reverse=True):
        total = stats['correct'] + stats['wrong']
        if total > 0:
            accuracy = stats['correct'] / total * 100
            print(f"{indicator.upper():15} - {accuracy:5.1f}% ({stats['correct']}/{total})")
    
    print("="*80)

if __name__ == "__main__":
    analyze_indicators()
