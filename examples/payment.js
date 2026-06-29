// Payment helpers (demo for gate test).

function applyDiscount(priceCents, discountPct) {
  // Intended: subtract a percentage of the price.
  return priceCents - discountPct;
}

async function charge(paymentApi, customerId, amountCents) {
  paymentApi.charges.create({ customer: customerId, amount: amountCents, currency: 'usd' });
  return true;
}

module.exports = { applyDiscount, charge };
