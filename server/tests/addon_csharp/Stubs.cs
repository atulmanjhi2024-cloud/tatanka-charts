// Stand-ins for the parts of NinjaTrader's API the add-on (ninjatrader/TatankaChartFeed.cs) uses, enough to compile it
// with mono and drive it (Harness.cs, tests/test_hunt5_journal.py).
using System;
using System.Collections.Generic;

namespace NinjaTrader.NinjaScript
{
	public enum State { SetDefaults, Active, Terminated }

	public abstract class AddOnBase
	{
		public string Name { get; set; }
		public string Description { get; set; }
		public State State { get; set; }
		protected virtual void OnStateChange() { }
		public void Print(string s) { Console.Error.WriteLine("[NT] " + s); }
	}
}

namespace NinjaTrader.Cbi
{
	public class Dispatcher
	{
		public static bool Defer;
		public static readonly List<Action> Pending = new List<Action>();
		public void InvokeAsync(Action a) { if (Defer) { lock (Pending) Pending.Add(a); } else a(); }
		public static void Pump() { List<Action> p; lock (Pending) { p = new List<Action>(Pending); Pending.Clear(); } foreach (Action a in p) a(); }
	}

	public class Rollover
	{
		public DateTime Date;
		public DateTime ContractMonth;
	}

	public class MasterInstrument
	{
		public string Name;
		public List<Rollover> RolloverCollection = new List<Rollover>();
		public object TradingHours = null;
		public double PointValue = 50;
		public double TickSize = 0.25;
		public double RoundToTickSize(double p) { return Math.Round(p / TickSize) * TickSize; }
	}

	public class Instrument
	{
		public static readonly Dictionary<string, Instrument> Known = new Dictionary<string, Instrument>();
		public string FullName;
		public MasterInstrument MasterInstrument;
		public DateTime Expiry;
		public Dispatcher Dispatcher = new Dispatcher();
		public static Instrument GetInstrument(string name) { Instrument i; return Known.TryGetValue(name, out i) ? i : null; }
	}

	public enum MarketPosition { Long, Short, Flat }
	public enum ConnectionStatus { Connected, Disconnected }
	public enum ErrorCode { NoError, Other }

	public class Execution
	{
		public Instrument Instrument;
		public int Quantity;
		public Account Account;
		public DateTime Time;
		public double Price, Commission;
		public MarketPosition MarketPosition;
		public string ExecutionId = "", OrderId = "", Name = "";
	}

	public class ExecutionEventArgs : EventArgs { public Execution Execution; }

	public enum Operation { Add, Update, Remove }

	public class Position
	{
		public Account Account;
		public Instrument Instrument;
		public MarketPosition MarketPosition;
		public int Quantity;
		public double AveragePrice;
	}

	public class PositionEventArgs : EventArgs
	{
		public Position Position;
		public MarketPosition MarketPosition;
		public int Quantity;
		public double AveragePrice;
		public Operation Operation;
	}

	public class Account
	{
		public static readonly List<Account> All = new List<Account>();
		public string Name = "Sim101";
		public List<Execution> Executions = new List<Execution>();
		public List<Position> Positions = new List<Position>();
		public event EventHandler<ExecutionEventArgs> ExecutionUpdate;
		public event EventHandler<PositionEventArgs> PositionUpdate;
		public void Fire(Execution x) { if (ExecutionUpdate != null) ExecutionUpdate(this, new ExecutionEventArgs { Execution = x }); }
		public void Fire(Position p) { if (PositionUpdate != null) PositionUpdate(this, new PositionEventArgs { Position = p, MarketPosition = p.MarketPosition, Quantity = p.Quantity, AveragePrice = p.AveragePrice }); }
	}

	public class ConnectionOptions { public string Provider = "Rithmic"; }

	public class Connection
	{
		public static Connection PlaybackConnection = null;
		public static readonly List<Connection> Connections = new List<Connection>();
		public ConnectionStatus Status, PriceStatus;
		public ConnectionOptions Options = new ConnectionOptions();
	}
}

namespace NinjaTrader.Data
{
	using NinjaTrader.Cbi;

	public enum MarketDataType { Last, Ask, Bid, LastClose, Settlement, DailyVolume, Opening }
	public enum BarsPeriodType { Tick, Minute }

	public class MarketDataEventArgs : EventArgs
	{
		public MarketDataType MarketDataType;
		public Instrument Instrument;
		public double Price, Ask, Bid;
		public long Volume;
		public DateTime Time = DateTime.Now;
	}

	public class MarketData
	{
		public static readonly List<MarketData> All = new List<MarketData>();
		public readonly Instrument Instrument;
		private EventHandler<MarketDataEventArgs> handlers;
		public int Subscribers { get { return handlers == null ? 0 : handlers.GetInvocationList().Length; } }
		public MarketData(Instrument i) { Instrument = i; lock (All) All.Add(this); }
		public event EventHandler<MarketDataEventArgs> Update
		{
			add { handlers += value; }
			remove { handlers -= value; }
		}
		public void Raise(MarketDataEventArgs e) { e.Instrument = Instrument; if (handlers != null) handlers(this, e); }
	}

	public class BarsPeriod { public BarsPeriodType BarsPeriodType; public int Value; }

	public class Bars
	{
		public int Count = 0;
		public DateTime GetTime(int i) { return DateTime.MinValue; }
		public long GetVolume(int i) { return 0; }
		public double GetOpen(int i) { return 0; }
		public double GetHigh(int i) { return 0; }
		public double GetLow(int i) { return 0; }
		public double GetClose(int i) { return 0; }
	}

	public class BarsRequest : IDisposable
	{
		public BarsPeriod BarsPeriod;
		public object TradingHours;
		public Bars Bars = new Bars();
		public BarsRequest(Instrument i, DateTime from, DateTime to) { }
		public void Request(Action<BarsRequest, ErrorCode, string> done) { done(this, ErrorCode.NoError, ""); }
		public void Dispose() { }
	}
}
