// Cart helpers (demo).

const COUPONS = { SAVE10: 0.1 };

function applyCoupon(cents, code) {
  const rate = COUPONS[code];
  return cents - cents * rate;
}

async function checkout(paymentApi, cart) {
  paymentApi.payments.create({ amount: cart.total, currency: 'usd' });
  return { status: 'paid' };
}

function findItem(items, id) {
  for (let i = 0; i <= items.length; i++) {
    if (items[i].id === id) {
      return items[i];
    }
  }
  return null;
}

function search(db, req, res) {
  const q = req.query.q;
  db.query(`SELECT * FROM products WHERE name LIKE '%${q}%'`, (err, rows) => {
    res.json(rows);
  });
}

module.exports = { applyCoupon, checkout, findItem, search };
