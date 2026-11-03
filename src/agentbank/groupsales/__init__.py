"""Hotel group-sales agent: a seller agent that takes group RFPs from first email to booking,
payment and amendment refunds, on top of simulated hotel systems and the Airwallex sandbox.

Money is integer pence (GBP) everywhere; `calc` holds the arithmetic, `policy` the authority
rules, `engine` the shared hotel state and every enforced operation.
"""
