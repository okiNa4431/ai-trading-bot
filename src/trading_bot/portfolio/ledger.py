"""Pure cash/position mutations with native-currency average-cost accounting."""

from trading_bot.portfolio.models import ZERO, Account, Execution, Position


def apply_execution(account: Account, execution: Execution, position: Position) -> Account:
    balances = dict(account.balances)
    holdings = dict(account.positions)
    realized = dict(account.realized_pnl)
    cost = execution.notional + execution.commission
    if execution.side == "BUY":
        if balances[execution.currency] < cost:
            raise ValueError("insufficient cash including fees")
        quantity = position.quantity + execution.quantity
        basis = position.cost_basis + cost
        average = basis / quantity
        balances[execution.currency] -= cost
    else:
        if position.quantity < execution.quantity:
            raise ValueError("short selling is forbidden")
        quantity = position.quantity - execution.quantity
        average = position.average_cost if quantity else ZERO
        removed = position.average_cost * execution.quantity if quantity else position.cost_basis
        basis = position.cost_basis - removed
        balances[execution.currency] += execution.notional - execution.commission
        realized[execution.currency] += execution.realized_pnl
    holdings[position.instrument.symbol] = Position(
        instrument=position.instrument,
        quantity=quantity,
        average_cost=average,
        cost_basis=basis,
    )
    return Account.model_validate(
        {
            **account.model_dump(),
            "balances": balances,
            "positions": holdings,
            "realized_pnl": realized,
            "last_event_at": execution.price_at
            if execution.mode == "historical"
            else execution.processed_at,
        }
    )
