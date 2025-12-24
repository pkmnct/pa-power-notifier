#!/usr/bin/env python3
"""
PA Power & Gas Rate Notifier
Periodically checks for potential savings on PA utilities
"""

import os
import csv
import json
import requests
import logging
from datetime import datetime
from typing import Dict, List, Optional
from io import StringIO
from dataclasses import dataclass, asdict

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


@dataclass
class Rate:
    """Represents a utility rate"""
    price: str
    supplier: str
    type: str
    cancellation_fee: str
    deposit_required: str
    discounts_incentives: str
    introductory_price: str
    monthly_fee: str
    more_info: str
    service_type: str
    term_end_date: str
    term_length: str
    utility_type: str  # 'electric' or 'gas'
    
    def get_numeric_price(self) -> Optional[float]:
        """Extract numeric price value for comparison"""
        try:
            # Remove $ and any whitespace, extract first number
            price_str = self.price.replace('$', '').replace(',', '').strip()
            # Handle cases like "0.0899 per kWh" or "$0.0899"
            parts = price_str.split()
            if parts:
                return float(parts[0])
        except (ValueError, AttributeError):
            pass
        return None


class RateChecker:
    """Checks rates from PA Power Switch and PA Gas Switch APIs"""
    
    def __init__(self):
        # Electric API configuration
        self.electric_api_url = "https://www.papowerswitch.com/umbraco/Api/ShopApi/RateCSV"
        self.electric_id = os.getenv('ELECTRIC_ID', '1180')
        self.electric_service_type = os.getenv('ELECTRIC_SERVICE_TYPE', 'residential')
        self.electric_rate_type = os.getenv('ELECTRIC_RATE_TYPE', 'RS - Regular Residential Service')
        
        # Gas API configuration
        self.gas_api_url = "https://www.pagasswitch.com/umbraco/Api/ShopApi/RateCSV"
        self.gas_id = os.getenv('GAS_ID', '4423')
        self.gas_service_type = os.getenv('GAS_SERVICE_TYPE', 'residential')
        
        # Current rates (for comparison)
        self.current_electric_price = float(os.getenv('CURRENT_ELECTRIC_PRICE', '0'))
        self.current_gas_price = float(os.getenv('CURRENT_GAS_PRICE', '0'))
        
        # Notification threshold (percentage savings to notify)
        self.notification_threshold = float(os.getenv('NOTIFICATION_THRESHOLD', '5.0'))
        
        # Storage file for previous rates
        self.storage_file = os.getenv('STORAGE_FILE', '/data/previous_rates.json')
        
    def fetch_electric_rates(self) -> List[Rate]:
        """Fetch electric rates from PA Power Switch API"""
        try:
            params = {
                'id': self.electric_id,
                'servicetype': self.electric_service_type,
                'ratetype': self.electric_rate_type
            }
            
            logger.info(f"Fetching electric rates with params: {params}")
            response = requests.get(self.electric_api_url, params=params, timeout=30)
            response.raise_for_status()
            
            # Parse CSV response
            csv_data = StringIO(response.text)
            reader = csv.DictReader(csv_data)
            
            rates = []
            for row in reader:
                rate = Rate(
                    price=row.get('Price', ''),
                    supplier=row.get('Supplier', ''),
                    type=row.get('Type', ''),
                    cancellation_fee=row.get('Cancellation Fee', ''),
                    deposit_required=row.get('Deposit Required', ''),
                    discounts_incentives=row.get('Discounts/Incentives Available', ''),
                    introductory_price=row.get('Introductory price', ''),
                    monthly_fee=row.get('Monthly Fee', ''),
                    more_info=row.get('More info', ''),
                    service_type=row.get('Service Type', ''),
                    term_end_date=row.get('Term End Date', ''),
                    term_length=row.get('Term Length', ''),
                    utility_type='electric'
                )
                rates.append(rate)
            
            logger.info(f"Fetched {len(rates)} electric rates")
            return rates
            
        except Exception as e:
            logger.error(f"Error fetching electric rates: {e}")
            return []
    
    def fetch_gas_rates(self) -> List[Rate]:
        """Fetch gas rates from PA Gas Switch API"""
        try:
            params = {
                'id': self.gas_id,
                'servicetype': self.gas_service_type
            }
            
            logger.info(f"Fetching gas rates with params: {params}")
            response = requests.get(self.gas_api_url, params=params, timeout=30)
            response.raise_for_status()
            
            # Parse CSV response
            csv_data = StringIO(response.text)
            reader = csv.DictReader(csv_data)
            
            rates = []
            for row in reader:
                rate = Rate(
                    price=row.get('Price', ''),
                    supplier=row.get('Supplier', ''),
                    type=row.get('Type', ''),
                    cancellation_fee=row.get('Cancellation Fee', ''),
                    deposit_required=row.get('Deposit Required', ''),
                    discounts_incentives=row.get('Discounts/Incentives Available', ''),
                    introductory_price=row.get('Introductory price', ''),
                    monthly_fee=row.get('Monthly Fee', ''),
                    more_info=row.get('More info', ''),
                    service_type=row.get('Service Type', ''),
                    term_end_date=row.get('Term End Date', ''),
                    term_length=row.get('Term Length', ''),
                    utility_type='gas'
                )
                rates.append(rate)
            
            logger.info(f"Fetched {len(rates)} gas rates")
            return rates
            
        except Exception as e:
            logger.error(f"Error fetching gas rates: {e}")
            return []
    
    def find_best_rates(self, rates: List[Rate]) -> List[Rate]:
        """Find the best (lowest) rates, sorted by price"""
        valid_rates = [r for r in rates if r.get_numeric_price() is not None]
        return sorted(valid_rates, key=lambda x: x.get_numeric_price())
    
    def calculate_savings(self, current_price: float, new_price: float) -> Dict:
        """Calculate savings percentage and amount"""
        if current_price == 0:
            return {'percentage': 0, 'amount': 0, 'message': 'Current price not set'}
        
        savings_amount = current_price - new_price
        savings_percentage = (savings_amount / current_price) * 100
        
        return {
            'percentage': savings_percentage,
            'amount': savings_amount,
            'message': f"{savings_percentage:.2f}% savings (${savings_amount:.4f} per unit)"
        }
    
    def check_for_savings(self, rates: List[Rate], current_price: float, utility_type: str) -> List[Dict]:
        """Check for potential savings and return opportunities"""
        opportunities = []
        
        if not rates:
            return opportunities
        
        best_rates = self.find_best_rates(rates)
        
        if not best_rates:
            logger.warning(f"No valid rates found for {utility_type}")
            return opportunities
        
        best_rate = best_rates[0]
        best_price = best_rate.get_numeric_price()
        
        if best_price is None:
            return opportunities
        
        savings = self.calculate_savings(current_price, best_price)
        
        if savings['percentage'] >= self.notification_threshold:
            opportunity = {
                'utility_type': utility_type,
                'current_price': current_price,
                'best_price': best_price,
                'best_supplier': best_rate.supplier,
                'best_rate_type': best_rate.type,
                'term_length': best_rate.term_length,
                'cancellation_fee': best_rate.cancellation_fee,
                'deposit_required': best_rate.deposit_required,
                'more_info': best_rate.more_info,
                'savings': savings,
                'timestamp': datetime.now().isoformat()
            }
            opportunities.append(opportunity)
            
            logger.info(f"Found {utility_type} savings opportunity: {savings['message']}")
        
        return opportunities
    
    def load_previous_rates(self) -> Dict:
        """Load previous rates from storage"""
        if os.path.exists(self.storage_file):
            try:
                with open(self.storage_file, 'r') as f:
                    return json.load(f)
            except Exception as e:
                logger.warning(f"Error loading previous rates: {e}")
        return {}
    
    def save_rates(self, electric_rates: List[Rate], gas_rates: List[Rate]):
        """Save current rates to storage"""
        try:
            os.makedirs(os.path.dirname(self.storage_file), exist_ok=True)
            
            data = {
                'electric': [asdict(r) for r in electric_rates],
                'gas': [asdict(r) for r in gas_rates],
                'timestamp': datetime.now().isoformat()
            }
            
            with open(self.storage_file, 'w') as f:
                json.dump(data, f, indent=2)
                
        except Exception as e:
            logger.error(f"Error saving rates: {e}")
    
    def format_notification(self, opportunities: List[Dict]) -> str:
        """Format notification message"""
        if not opportunities:
            return "No significant savings opportunities found."
        
        message = "🔌 PA Utility Savings Opportunities Found!\n\n"
        
        for opp in opportunities:
            message += f"⚡ {opp['utility_type'].upper()} Savings Available:\n"
            message += f"  Current Price: ${opp['current_price']:.4f}\n"
            message += f"  Best Price: ${opp['best_price']:.4f}\n"
            message += f"  Supplier: {opp['best_supplier']}\n"
            message += f"  Rate Type: {opp['best_rate_type']}\n"
            message += f"  Term Length: {opp['term_length']}\n"
            message += f"  Savings: {opp['savings']['message']}\n"
            if opp['cancellation_fee']:
                message += f"  Cancellation Fee: {opp['cancellation_fee']}\n"
            if opp['deposit_required']:
                message += f"  Deposit Required: {opp['deposit_required']}\n"
            message += "\n"
        
        return message
    
    def run_check(self):
        """Run a complete check cycle"""
        logger.info("Starting rate check...")
        
        # Fetch rates
        electric_rates = self.fetch_electric_rates()
        gas_rates = self.fetch_gas_rates()
        
        # Save rates for future comparison
        self.save_rates(electric_rates, gas_rates)
        
        # Check for savings opportunities
        opportunities = []
        
        if self.current_electric_price > 0:
            electric_opps = self.check_for_savings(electric_rates, self.current_electric_price, 'electric')
            opportunities.extend(electric_opps)
        
        if self.current_gas_price > 0:
            gas_opps = self.check_for_savings(gas_rates, self.current_gas_price, 'gas')
            opportunities.extend(gas_opps)
        
        # Print results
        if opportunities:
            notification = self.format_notification(opportunities)
            print("\n" + "="*60)
            print(notification)
            print("="*60 + "\n")
            logger.info(f"Found {len(opportunities)} savings opportunities")
        else:
            logger.info("No significant savings opportunities found")
        
        # Also print current best rates even if below threshold
        if electric_rates:
            best_electric = self.find_best_rates(electric_rates)
            if best_electric:
                logger.info(f"Current best electric rate: ${best_electric[0].get_numeric_price():.4f} from {best_electric[0].supplier}")
        
        if gas_rates:
            best_gas = self.find_best_rates(gas_rates)
            if best_gas:
                logger.info(f"Current best gas rate: ${best_gas[0].get_numeric_price():.4f} from {best_gas[0].supplier}")
        
        return opportunities


def main():
    """Main entry point"""
    checker = RateChecker()
    checker.run_check()


if __name__ == '__main__':
    main()

