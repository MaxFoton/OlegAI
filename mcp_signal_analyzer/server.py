#!/usr/bin/env python3
"""
MCP Server for Crypto Signal Analysis
Simplified version compatible with MCP 2.0
"""

import json
import logging
import re
from datetime import datetime
from typing import Any

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool, ListToolsResult, CallToolResult, ListToolsRequest, CallToolRequest

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("signal-analyzer-mcp")

# Global state
latest_signals = []
MAX_SIGNALS_HISTORY = 50


class SignalParser:
    """Parse trading signals from text messages"""
    
    @staticmethod
    def parse_signal(text: str) -> dict | None:
        """Parse signal text into structured data"""
        try:
            signal = {}
            
            # Extract symbol
            symbol_match = re.search(r'📢\s*(\w+)', text)
            if symbol_match:
                signal['symbol'] = symbol_match.group(1)
            
            # Extract price
            price_match = re.search(r'price=\$?([\d.]+)', text)
            if price_match:
                signal['price'] = float(price_match.group(1))
            
            # Extract volume multiplier
            vol_match = re.search(r'vol×([\d.]+)', text)
            if vol_match:
                signal['vol_multiplier'] = float(vol_match.group(1))
            
            # Extract z-score
            z_match = re.search(r'z=([\d.]+)', text)
            if z_match:
                signal['z_score'] = float(z_match.group(1))
            
            # Extract timeframe changes
            tf5m_match = re.search(r'5m:\s*([+\-][\d.]+)%', text)
            if tf5m_match:
                signal['change_5m'] = float(tf5m_match.group(1))
            
            tf15m_match = re.search(r'15m:\s*([+\-][\d.]+)%', text)
            if tf15m_match:
                signal['change_15m'] = float(tf15m_match.group(1))
            
            # Extract phase (НОВЫЙ формат: "фаза EARLY_EXPANSION")
            phase_match = re.search(r'(?:фаза|Phase:)\s*(\w+)', text)
            if phase_match:
                signal['phase'] = phase_match.group(1)
            
            # Extract RSI (НОВЫЙ формат: "RSI: 5m 47 / 1h 43")
            rsi_match = re.search(r'RSI:.*?1h\s+(\d+)', text)
            if not rsi_match:
                rsi_match = re.search(r'RSI:.*?5m\s+(\d+)', text)
            if rsi_match:
                signal['rsi'] = float(rsi_match.group(1))
            
            # Extract velocity
            vel_match = re.search(r'vel:\s*([+\-][\d.]+)%', text)
            if vel_match:
                signal['velocity'] = float(vel_match.group(1))
            
            # Extract Lorentzian (НОВЫЙ формат: "Lor: -1 (3%)")
            lor_match = re.search(r'Lor:\s*([+\-]?\d+)\s*\((\d+)%\)', text)
            if lor_match:
                signal['lorentzian'] = {
                    'value': int(lor_match.group(1)),
                    'prediction': int(lor_match.group(1)),  # В новом формате нет отдельного pred
                    'strength': float(lor_match.group(2))
                }
            
            # Extract OBV/Absorption (НОВЫЙ формат: "OBV накопление" или "OBV распределение")
            if 'OBV накопление' in text or 'OBV +1' in text:
                signal['absorption'] = 'LONG'
            elif 'OBV распределение' in text or 'OBV -1' in text:
                signal['absorption'] = 'SHORT'
            else:
                absorption_match = re.search(r'Absorption:\s*(\w+)?', text)
                if absorption_match:
                    signal['absorption'] = absorption_match.group(1) if absorption_match.group(1) else None
            
            # Extract VWAP
            vwap_match = re.search(r'VWAP:\s*([+\-][\d.]+)%\s*\((\w+)\)', text)
            if vwap_match:
                signal['vwap'] = {
                    'deviation': float(vwap_match.group(1)),
                    'position': vwap_match.group(2)
                }
            
            # Extract VW-MACD
            macd_match = re.search(r'VW-MACD:\s*(\w+)', text)
            if macd_match:
                signal['vw_macd'] = macd_match.group(1)
            
            # Extract EMA
            ema_match = re.search(r'EMA:\s*(\w+)\s*\(([+\-][\d.]+)%\)', text)
            if ema_match:
                signal['ema'] = {
                    'trend': ema_match.group(1),
                    'value': float(ema_match.group(2))
                }
            
            # Extract OI
            oi_match = re.search(r'OI:\s*\$?([\d,]+)\s*\(([+\-][\d.]+)%', text)
            if oi_match:
                signal['open_interest'] = {
                    'value': float(oi_match.group(1).replace(',', '')),
                    'change_1h': float(oi_match.group(2))
                }
            
            # Extract BB trend
            bb_match = re.search(r'BB\s*1h\s*trend:\s*([+\-][\d.]+)%', text)
            if bb_match:
                signal['bb_trend'] = float(bb_match.group(1))
            
            # Extract signal type and score
            signal_match = re.search(r'(Сильный|Слабый)\s*сигнал\s*(LONG|SHORT)\s*\|\s*score=([\d]+)/([\d]+)', text)
            if signal_match:
                signal['signal_strength'] = signal_match.group(1)
                signal['signal_type'] = signal_match.group(2)
                signal['score'] = int(signal_match.group(3))
                signal['score_max'] = int(signal_match.group(4))
            
            # Extract volume in dollars
            volume_match = re.search(r'объём\s*\$?([\d,]+)', text)
            if volume_match:
                signal['volume_usd'] = float(volume_match.group(1).replace(',', ''))
            
            signal['raw_text'] = text
            signal['timestamp'] = datetime.utcnow().isoformat()
            
            return signal if 'symbol' in signal else None
            
        except Exception as e:
            logger.error(f"Parse error: {e}")
            return None


class SignalAnalyzer:
    """Analyze trading signals"""
    
    @staticmethod
    def analyze(signal: dict) -> dict:
        """Analyze signal and return verdict"""
        # DEBUG: Логируем входные данные
        logger.info(f"🔍 SERVER.PY ANALYZE INPUT: {json.dumps(signal, indent=2)}")
        
        analysis = {
            'symbol': signal.get('symbol'),
            'signal_type': signal.get('signal_type'),
            'score': signal.get('score', 0),
            'verdict': None,
            'confidence': 0,
            'recommendation': 'WAIT',
            'reasons_for': [],
            'reasons_against': [],
            'entry_params': {},
            'wait_for': [],
            'risk_level': 'MEDIUM'
        }
        
        rsi = signal.get('rsi', 50)
        phase = signal.get('phase', '')
        vwap = signal.get('vwap', {})
        vw_macd = signal.get('vw_macd', '')
        ema = signal.get('ema', {})
        bb_trend = signal.get('bb_trend', 0)
        absorption = signal.get('absorption')
        lorentzian = signal.get('lorentzian', {})
        vol_multiplier = signal.get('vol_multiplier', 1)
        
        # Предсказание модели LightGBM
        model_direction = signal.get('model_direction')
        model_confidence = signal.get('model_confidence', 0)
        
        signal_type = signal.get('signal_type', '').upper()
        
        # Analyze LONG
        if signal_type == 'LONG':
            # RSI анализ
            if rsi > 75:
                analysis['reasons_against'].append(f"🛑 RSI={rsi} ЭКСТРЕМАЛЬНО ПЕРЕКУПЛЕН - высокий риск ОТКАТА!")
            elif rsi < 30:
                analysis['reasons_for'].append(f"✅ RSI={rsi} перепродан")
            elif rsi < 40:
                analysis['reasons_for'].append(f"✅ RSI={rsi} в зоне для LONG")
            
            # Phase анализ
            if phase == 'ACCUMULATION':
                analysis['reasons_for'].append(f"🔥 Phase={phase} (НАКОПЛЕНИЕ - ДНО для LONG)")
            elif phase == 'EARLY_EXPANSION':
                analysis['reasons_for'].append(f"✅ Phase={phase} ранний рост")
            elif phase == 'LATE_EXPANSION':
                analysis['reasons_against'].append(f"⚠️ Phase={phase} поздний рост")
            elif phase == 'EXHAUSTION':
                analysis['reasons_against'].append(f"⚠️ Phase={phase} истощение")
            
            # Absorption  
            if absorption == 'SHORT':
                analysis['reasons_for'].append("✅ Шорты ликвидируются (OBV +1)")
            elif absorption == 'LONG' and rsi > 65:
                analysis['reasons_against'].append("⚠️ OBV +1 но RSI высокий - уже идет закупка")
            
            # VWAP
            vwap_dev = vwap.get('deviation', 0)
            if vwap_dev < -1.0:
                analysis['reasons_for'].append(f"✅ Под VWAP ({vwap_dev:.1f}%)")
            elif vwap_dev > 1.5:
                analysis['reasons_against'].append(f"⚠️ Далеко над VWAP (+{vwap_dev:.1f}%) - риск отката")
            
            # VW-MACD
            raw_text = signal.get('raw_text', '')
            if 'MACD: bearish' in raw_text:
                analysis['reasons_against'].append("🛑 MACD bearish ПРОТИВ LONG")
            elif 'MACD: bullish' in raw_text:
                analysis['reasons_for'].append("✅ MACD bullish ЗА LONG")
            
            # Lorentzian
            lor_pred = lorentzian.get('prediction', 0)
            if lor_pred < 0:
                analysis['reasons_against'].append(f"🛑 Lorentzian pred={lor_pred} ПРОТИВ LONG")
            elif lor_pred > 1:
                analysis['reasons_for'].append(f"✅ Lorentzian pred=+{lor_pred} ЗА LONG")
            
            # Volume
            if vol_multiplier > 5:
                analysis['reasons_for'].append(f"✅ Объём ×{vol_multiplier} - сильное движение")
            
            # Модель LightGBM - только как один из факторов, НЕ решающее слово
            if model_direction and model_confidence > 0.50:
                if model_direction == 'LONG':
                    analysis['reasons_for'].append(f"✅ ✅ LightGBM модель: LONG ({model_confidence:.0%})")
                else:
                    analysis['reasons_against'].append(f"⚠️ Модель предлагает: {model_direction} ({model_confidence:.0%})")
            
            # Считаем confidence на основе ВСЕХ факторов (модель = обычный фактор)
            confidence = 5 + len(analysis['reasons_for']) * 2 - len(analysis['reasons_against']) * 2
            
            # Экстремальные условия (блокирующие)
            if rsi > 80:
                confidence -= 5
                analysis['reasons_against'].append("🛑 Экстремально перекуплен - НЕ покупать на пике!")
            
            # Снижаем порог для ВХОДИТЬ (было 8, теперь 6)
            if confidence >= 6:
                analysis['recommendation'] = 'AGGRESSIVE_LONG'
                analysis['verdict'] = '✅ ВХОДИТЬ'
            elif confidence >= 3:
                analysis['recommendation'] = 'CONSERVATIVE_LONG'
                analysis['verdict'] = '⏳ ЖДАТЬ ПОДТВЕРЖДЕНИЯ'
                analysis['wait_for'] = ['MACD → bullish', 'RSI > 35', 'VWAP пробой вверх']
            else:
                analysis['recommendation'] = 'WAIT'
                analysis['verdict'] = '🚫 НЕ ВХОДИТЬ'
                if rsi > 75:
                    analysis['verdict'] += '\n\n❌ LONG на пике после пампа - это вход на откат вниз!'
                analysis['verdict'] += f'\n❌ Слишком много против (confidence={confidence}/10)'
                analysis['verdict'] += '\n🛑 Главные индикаторы против входа'
            
            analysis['confidence'] = max(-10, min(10, int(confidence)))
            
            price = signal.get('price', 0)
            if price > 0:
                analysis['entry_params'] = {
                    'entry_aggressive': round(price * 1.002, 8),
                    'entry_conservative': round(price * 1.007, 8),
                    'stop_loss': round(price * 0.98, 8),
                    'take_profit': round(price * 1.03, 8),
                    'position_size': '1-2%' if confidence < 7 else '2-3%'
                }
        
        # Analyze SHORT
        elif signal_type == 'SHORT':
            # RSI анализ
            if rsi < 30:
                analysis['reasons_against'].append(f"🛑 RSI={rsi} ЭКСТРЕМАЛЬНО ПЕРЕПРОДАН - высокий риск ОТСКОКА!")
            elif rsi > 70:
                analysis['reasons_for'].append(f"✅ RSI={rsi} перекуплен")
            elif rsi > 60:
                analysis['reasons_for'].append(f"✅ RSI={rsi} в зоне для SHORT")
            
            # Phase анализ
            if phase == 'LATE_EXPANSION':
                analysis['reasons_for'].append(f"🔥 Phase={phase} (РАСПРЕДЕЛЕНИЕ - ТОП для SHORT)")
            elif phase == 'DISTRIBUTION':
                analysis['reasons_for'].append(f"✅ Phase={phase} вершина")
            elif phase == 'EXHAUSTION':
                analysis['reasons_for'].append(f"✅ Phase={phase} истощение роста")
            elif phase == 'EARLY_EXPANSION':
                analysis['reasons_against'].append(f"⚠️ Phase={phase} ранний рост")
            
            # Absorption
            if absorption == 'LONG':
                analysis['reasons_for'].append("✅ Лонги ликвидируются (OBV -1)")
            elif absorption == 'SHORT' and rsi < 40:
                analysis['reasons_against'].append("⚠️ OBV -1 но RSI низкий - уже идет распродажа")
            
            # VWAP
            vwap_dev = vwap.get('deviation', 0)
            if vwap_dev > 1.0:
                analysis['reasons_for'].append(f"✅ Выше VWAP (+{vwap_dev:.1f}%)")
            elif vwap_dev < -1.5:
                analysis['reasons_against'].append(f"⚠️ Далеко под VWAP ({vwap_dev:.1f}%) - риск отскока")
            
            # VW-MACD (ищем "MACD: bearish/bullish" в тексте)
            raw_text = signal.get('raw_text', '')
            if 'MACD: bullish' in raw_text:
                analysis['reasons_against'].append("🛑 MACD bullish ПРОТИВ SHORT")
            elif 'MACD: bearish' in raw_text:
                analysis['reasons_for'].append("✅ MACD bearish ЗА SHORT")
            
            # Lorentzian
            lor_pred = lorentzian.get('prediction', 0)
            if lor_pred > 0:
                analysis['reasons_against'].append(f"🛑 Lorentzian pred=+{lor_pred} ПРОТИВ SHORT")
            elif lor_pred < -1:
                analysis['reasons_for'].append(f"✅ Lorentzian pred={lor_pred} ЗА SHORT")
            
            # Volume
            if vol_multiplier > 5:
                analysis['reasons_for'].append(f"✅ Объём ×{vol_multiplier} - сильное движение")
            
            # Модель LightGBM - только как один из факторов, НЕ решающее слово
            if model_direction and model_confidence > 0.50:
                if model_direction == 'SHORT':
                    analysis['reasons_for'].append(f"✅ ✅ LightGBM модель: SHORT ({model_confidence:.0%})")
                else:
                    analysis['reasons_against'].append(f"⚠️ Модель предлагает: {model_direction} ({model_confidence:.0%})")
            
            # Считаем confidence на основе ВСЕХ факторов (модель = обычный фактор)
            confidence = 5 + len(analysis['reasons_for']) * 2 - len(analysis['reasons_against']) * 2
            
            # Экстремальные условия (блокирующие)
            if rsi < 25:
                confidence -= 5
                analysis['reasons_against'].append("🛑 Экстремально перепродан - НЕ шортить на дне!")
            
            # Снижаем порог для ШОРТИТЬ (было 8, теперь 6)
            if confidence >= 6:
                analysis['recommendation'] = 'AGGRESSIVE_SHORT'
                analysis['verdict'] = '✅ ШОРТИТЬ'
            elif confidence >= 3:
                analysis['recommendation'] = 'CONSERVATIVE_SHORT'
                analysis['verdict'] = '⏳ ЖДАТЬ ПОДТВЕРЖДЕНИЯ'
                analysis['wait_for'] = ['MACD → bearish', 'RSI > 60', 'VWAP пробой вниз']
            else:
                analysis['recommendation'] = 'WAIT'
                analysis['verdict'] = '🚫 НЕ ШОРТИТЬ'
                if rsi < 30:
                    analysis['verdict'] += '\n\n❌ SHORT на дне после дампа - это вход на отскок вверх!'
                analysis['verdict'] += f'\n❌ Слишком много против (confidence={confidence}/10)'
                analysis['verdict'] += '\n🛑 Главные индикаторы против входа'
            
            analysis['confidence'] = max(-10, min(10, int(confidence)))
            
            price = signal.get('price', 0)
            if price > 0:
                analysis['entry_params'] = {
                    'entry_aggressive': round(price * 0.998, 8),
                    'entry_conservative': round(price * 0.993, 8),
                    'stop_loss': round(price * 1.03, 8),
                    'take_profit': round(price * 0.97, 8),
                    'position_size': '0.5-1%' if confidence < 7 else '1-2%'
                }
        
        if analysis['confidence'] >= 7:
            analysis['risk_level'] = 'MEDIUM'
        elif analysis['confidence'] >= 5:
            analysis['risk_level'] = 'HIGH'
        else:
            analysis['risk_level'] = 'VERY_HIGH'
        
        return analysis
    
    @staticmethod
    def format_analysis(signal: dict, analysis: dict) -> str:
        """Format analysis"""
        lines = [
            f"🔔 {analysis['symbol']} {analysis['signal_type']} Analysis",
            "━━━━━━━━━━━━━━━━━━━━━━━",
            f"📊 Score: {analysis['score']}/15",
            f"🎯 Вердикт: {analysis['verdict']} ({analysis['confidence']}/10)",
            f"⚠️ Риск: {analysis['risk_level']}",
            ""
        ]
        
        if analysis['reasons_for']:
            lines.append("✅ ЗА:")
            for r in analysis['reasons_for']:
                lines.append(f"  • {r}")
            lines.append("")
        
        if analysis['reasons_against']:
            lines.append("❌ ПРОТИВ:")
            for r in analysis['reasons_against']:
                lines.append(f"  • {r}")
            lines.append("")
        
        if analysis['entry_params']:
            p = analysis['entry_params']
            lines.extend([
                "💰 Параметры:",
                f"  Entry (агр): ${p.get('entry_aggressive', 'N/A')}",
                f"  Entry (конс): ${p.get('entry_conservative', 'N/A')}",
                f"  Stop: ${p.get('stop_loss', 'N/A')}",
                f"  Target: ${p.get('take_profit', 'N/A')}",
                f"  Size: {p.get('position_size', 'N/A')}",
                ""
            ])
        
        if analysis['wait_for']:
            lines.append("⏰ Ждать:")
            for w in analysis['wait_for']:
                lines.append(f"  • {w}")
        
        return "\n".join(lines)


# MCP Server
app = Server("signal-analyzer")


async def handle_list_tools(params=None) -> ListToolsResult:
    """List available tools"""
    return ListToolsResult(tools=[
        Tool(
            name="analyze_signal",
            description="Анализирует торговый сигнал от DEX Scanner. Извлекает данные (symbol, price, RSI, OI, volume) и выдаёт рекомендацию (LONG/SHORT/NEUTRAL), причины за/против, параметры входа (entry, stop, target).",
            inputSchema={
                "type": "object",
                "properties": {
                    "signal_text": {
                        "type": "string",
                        "description": "Текст сигнала от DEX Scanner (со всеми эмодзи и данными)"
                    }
                },
                "required": ["signal_text"]
            }
        ),
        Tool(
            name="get_latest_signals",
            description="Возвращает последние N сигналов из истории",
            inputSchema={
                "type": "object",
                "properties": {
                    "limit": {
                        "type": "number",
                        "description": "Количество сигналов (по умолчанию 5)"
                    }
                }
            }
        )
    ])


async def handle_call_tool(params) -> CallToolResult:
    """Handle tool calls"""
    name = params.name
    arguments = params.arguments or {}
    
    if name == "analyze_signal":
        signal_text = arguments.get("signal_text", "")
        signal = SignalParser.parse_signal(signal_text)
        
        if not signal:
            return CallToolResult(content=[TextContent(
                type="text",
                text="❌ Не удалось распарсить сигнал"
            )])
        
        latest_signals.append(signal)
        if len(latest_signals) > MAX_SIGNALS_HISTORY:
            latest_signals.pop(0)
        
        analysis = SignalAnalyzer.analyze(signal)
        formatted = SignalAnalyzer.format_analysis(signal, analysis)
        
        return CallToolResult(content=[TextContent(type="text", text=formatted)])
    
    elif name == "get_latest_signals":
        limit = arguments.get("limit", 5)
        signals = latest_signals[-limit:]
        
        if not signals:
            return CallToolResult(content=[TextContent(type="text", text="📭 Нет сигналов")])
        
        result = f"📊 Последние {len(signals)} сигналов:\n\n"
        for i, sig in enumerate(reversed(signals), 1):
            result += f"{i}. {sig.get('symbol')} {sig.get('signal_type')} (score={sig.get('score')}/15)\n"
        
        return CallToolResult(content=[TextContent(type="text", text=result)])
    
    return CallToolResult(content=[TextContent(type="text", text=f"Unknown tool: {name}")])


# Register handlers
app.add_request_handler("tools/list", type(None), handle_list_tools)
app.add_request_handler("tools/call", CallToolRequest, handle_call_tool)


async def main():
    """Run server"""
    async with stdio_server() as (read_stream, write_stream):
        await app.run(read_stream, write_stream, app.create_initialization_options())


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
