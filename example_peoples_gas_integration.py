#!/usr/bin/env python3
"""
Example: Integrating People's Gas Analyzer with PA Gas Switch Rate Checker

This example shows how to:
1. Fetch current usage from People's Gas API
2. Use the usage data to compare with PA Gas Switch plans
3. Calculate projected savings for different plans
"""

import os
import logging

try:
    from env_bootstrap import load_dotenv_if_present

    load_dotenv_if_present()
except ImportError:
    pass

from pa_power_notifier import RateChecker, Rate
from peoples_gas_analyzer import PeoplesGasAnalyzer
from gas_usage_forecast import forecast_annual_from_calendar_profile, parse_money_field

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


def compare_gas_plans_with_peoples_usage():
    """Compare PA Gas Switch plans using People's Gas usage data"""
    
    # Initialize analyzers
    # Credentials from PEOPLES_GAS_USERNAME / PEOPLES_GAS_PASSWORD env vars
    peoples_analyzer = PeoplesGasAnalyzer()
    rate_checker = RateChecker()

    # Get People's Gas usage analysis (fetches usage + billing history automatically)
    logger.info("Analyzing People's Gas usage...")
    peoples_results = peoples_analyzer.run_analysis()
    
    if not peoples_results or not peoples_results.get('usage_analysis'):
        logger.error("Could not get People's Gas usage data")
        return
    
    usage_analysis = peoples_results['usage_analysis']
    current_rate_per_mcf = peoples_results["current_effective_rate"]
    usage_rows = peoples_results.get("usage_data") or []

    fc = forecast_annual_from_calendar_profile(
        usage_rows,
        aggregator=os.environ.get("GAS_PROFILE_AGGREGATOR", "median"),
    )
    
    print("\n" + "="*70)
    print("People's Gas Usage Analysis")
    print("="*70)
    
    if current_rate_per_mcf:
        print(f"Current Effective Rate (blend): ${current_rate_per_mcf:.4f}/MCF")
    else:
        print("Current Effective Rate: Not available")

    print(f"Average Monthly Usage: {usage_analysis.average_monthly_usage_mcf:.1f} MCF")
    print(
        "Forecast annual (calendar-month median profile): "
        f"{fc.annual_mcf:.1f} MCF / {fc.annual_therms:.0f} therms "
        f"[trailing-12 sum MCF: {fc.trailing_twelve_months_mcf}]"
    )
    print(f"Naive annual MCF (= avg×12): {usage_analysis.average_monthly_usage_mcf * 12:.1f} MCF")

    if usage_analysis.average_monthly_cost:
        print(f"Average Monthly Cost: ${usage_analysis.average_monthly_cost:.2f}")
        print(f"Projected Annual Cost: ${usage_analysis.average_monthly_cost * 12:.2f}")
    
    print("="*70 + "\n")
    
    # Fetch available plans from PA Gas Switch
    logger.info("Fetching available plans from PA Gas Switch...")
    available_plans = rate_checker.fetch_gas_rates()
    
    if not available_plans:
        logger.error("Could not fetch plans from PA Gas Switch")
        return
    
    # Compare each plan
    print("\n" + "="*70)
    print("Plan Comparison Based on Your Usage")
    print("="*70)
    
    comparisons = []
    
    for plan in available_plans:
        plan_rate = plan.get_numeric_price()
        if plan_rate is None:
            continue

        fee = parse_money_field(plan.monthly_fee)
        annual_supplier_cost = fc.annual_therms * plan_rate + fee * 12

        comparisons.append({
            'plan': plan,
            'rate': plan_rate,
            'annual_supplier_cost_approx': annual_supplier_cost,
        })
    
    comparisons.sort(key=lambda x: x['annual_supplier_cost_approx'])

    print(f"\n{'Rank':<6} {'Supplier':<22} {'$/therm':<12} {'Annual supplier (~)':<20}")
    print("-" * 70)

    for i, comp in enumerate(comparisons[:10], 1):  # Show top 10
        plan = comp['plan']
        plan_rate = comp['rate']
        annual_approx = comp['annual_supplier_cost_approx']

        print(
            f"{i:<6} {plan.supplier[:21]:<22} ${plan_rate:.6f}  ${annual_approx:,.2f}"
        )
    
    print("\n" + "="*70)
    
    # Show detailed comparison for top 3
    print("\nTop 3 Plans - Detailed Comparison:")
    print("="*70)
    
    for i, comp in enumerate(comparisons[:3], 1):
        plan = comp['plan']

        print(f"\n{i}. {plan.supplier} - {plan.type}")
        print(f"   Rate: ${comp['rate']:.6f} per therm (CSV); monthly fee parsed: ${parse_money_field(plan.monthly_fee):.2f}")
        print(f"   Estimated annual supplier charge (forecast therms × rate + fees): ${comp['annual_supplier_cost_approx']:.2f}")

        print(f"   Term Length: {plan.term_length}")
        if plan.cancellation_fee:
            print(f"   Cancellation Fee: {plan.cancellation_fee}")
        if plan.more_info:
            print(f"   More Info: {plan.more_info}")
    
    print("="*70 + "\n")
    
    if not usage_analysis.average_monthly_cost:
        print("\nTip: Billing data could not be fetched. Set PEOPLES_GAS_USERNAME and "
              "PEOPLES_GAS_PASSWORD to enable automatic rate calculation.\n")


if __name__ == '__main__':
    # Set environment variables if not already set
    # These can also be set via .env file or docker-compose
    
    # People's Gas API configuration
    # os.environ['PEOPLES_GAS_USE_FILE'] = 'true'  # Use gasResponse.json file instead of API
    
    # PA Gas Switch configuration
    # os.environ['GAS_ID'] = '4423'
    # os.environ['GAS_SERVICE_TYPE'] = 'residential'
    
    compare_gas_plans_with_peoples_usage()

