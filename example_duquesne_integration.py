#!/usr/bin/env python3
"""
Example: Integrating Duquesne Light Analyzer with PA Power Switch Rate Checker

This example shows how to:
1. Fetch current usage and rates from Duquesne Light API
2. Use the inferred effective rate to compare with PA Power Switch plans
3. Calculate projected savings for different plans
"""

import os
import logging
from pa_power_notifier import RateChecker, Rate
from duquesne_light_analyzer import DuquesneLightAnalyzer

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


def compare_plans_with_duquesne_usage():
    """Compare PA Power Switch plans using Duquesne Light usage data"""
    
    # Initialize analyzers
    duquesne_analyzer = DuquesneLightAnalyzer()
    rate_checker = RateChecker()
    
    # Get Duquesne Light usage analysis
    logger.info("Analyzing Duquesne Light usage...")
    duquesne_results = duquesne_analyzer.run_analysis()
    
    if not duquesne_results or not duquesne_results.get('usage_analysis'):
        logger.error("Could not get Duquesne Light usage data")
        return
    
    usage_analysis = duquesne_results['usage_analysis']
    current_rate = duquesne_results['current_effective_rate']
    
    print("\n" + "="*70)
    print("Duquesne Light Usage Analysis")
    print("="*70)
    print(f"Current Effective Rate: ${current_rate:.6f} per kWh")
    print(f"Average Daily Usage: {usage_analysis.average_daily_kwh:.2f} kWh")
    print(f"Average Daily Cost: ${usage_analysis.average_daily_cost:.2f}")
    print(f"Projected Monthly Usage: {usage_analysis.average_daily_kwh * 30:.2f} kWh")
    print(f"Projected Monthly Cost: ${usage_analysis.average_daily_cost * 30:.2f}")
    print("="*70 + "\n")
    
    # Fetch available plans from PA Power Switch
    logger.info("Fetching available plans from PA Power Switch...")
    available_plans = rate_checker.fetch_electric_rates()
    
    if not available_plans:
        logger.error("Could not fetch plans from PA Power Switch")
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
        
        projection = duquesne_analyzer.calculate_projected_cost(usage_analysis, plan_rate)
        
        comparisons.append({
            'plan': plan,
            'rate': plan_rate,
            'projection': projection
        })
    
    # Sort by annual savings
    comparisons.sort(key=lambda x: x['projection']['annual_savings'], reverse=True)
    
    print(f"\n{'Rank':<6} {'Supplier':<25} {'Rate':<12} {'Monthly':<12} {'Annual':<12} {'Savings':<12}")
    print("-" * 70)
    
    for i, comp in enumerate(comparisons[:10], 1):  # Show top 10
        plan = comp['plan']
        plan_rate = comp['rate']
        proj = comp['projection']
        
        savings_str = f"${proj['annual_savings']:.2f}" if proj['annual_savings'] > 0 else f"-${abs(proj['annual_savings']):.2f}"
        
        print(f"{i:<6} {plan.supplier[:24]:<25} ${plan_rate:.6f} ${proj['monthly_cost']:.2f} ${proj['annual_cost']:.2f} {savings_str}")
    
    print("\n" + "="*70)
    
    # Show detailed comparison for top 3
    print("\nTop 3 Plans - Detailed Comparison:")
    print("="*70)
    
    for i, comp in enumerate(comparisons[:3], 1):
        plan = comp['plan']
        proj = comp['projection']
        
        print(f"\n{i}. {plan.supplier} - {plan.type}")
        print(f"   Rate: ${comp['rate']:.6f} per kWh")
        print(f"   Monthly Cost: ${proj['monthly_cost']:.2f} (vs current ${proj['current_monthly_cost']:.2f})")
        print(f"   Annual Cost: ${proj['annual_cost']:.2f} (vs current ${proj['current_annual_cost']:.2f})")
        
        if proj['annual_savings'] > 0:
            print(f"   💰 Annual Savings: ${proj['annual_savings']:.2f} ({proj['annual_savings_percent']:.1f}%)")
        else:
            print(f"   ⚠️  Annual Cost Increase: ${abs(proj['annual_savings']):.2f} ({abs(proj['annual_savings_percent']):.1f}%)")
        
        print(f"   Term Length: {plan.term_length}")
        if plan.cancellation_fee:
            print(f"   Cancellation Fee: {plan.cancellation_fee}")
        if plan.more_info:
            print(f"   More Info: {plan.more_info}")
    
    print("="*70 + "\n")


if __name__ == '__main__':
    # Set environment variables if not already set
    # These can also be set via .env file or docker-compose
    
    # Duquesne Light API credentials (optional - can use response.json file instead)
    # os.environ['DUQUESNE_CUSTOMER_URN'] = 'urn:opower:customer:uuid:...'
    # os.environ['DUQUESNE_UTILITY_ACCOUNT_UUID'] = '...'
    # os.environ['DUQUESNE_USE_FILE'] = 'true'  # Use response.json file instead of API
    
    # PA Power Switch configuration
    # os.environ['ELECTRIC_ID'] = '1180'
    # os.environ['ELECTRIC_SERVICE_TYPE'] = 'residential'
    # os.environ['ELECTRIC_RATE_TYPE'] = 'RS - Regular Residential Service'
    
    compare_plans_with_duquesne_usage()

