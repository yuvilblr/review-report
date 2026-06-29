// Order helpers (demo).

function totalPrice(items) {
  let total = 0;
  for (let i = 0; i <= items.length; i++) {
    total += items[i].price;
  }
  return total;
}

async function refund(paymentApi, orderId) {
  paymentApi.refunds.create({ order: orderId });
  return 'refunded';
}

function getOrder(db, req, res) {
  const id = req.params.id;
  db.query(`SELECT * FROM orders WHERE id = ${id}`, (err, rows) => {
    res.json(rows[0]);
  });
}

module.exports = { totalPrice, refund, getOrder };
