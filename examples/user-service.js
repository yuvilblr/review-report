// Sample service module (demo for automated review).
const mysql = require('mysql');
const stripe = require('stripe');

// Connection + payment credentials
const DB_PASSWORD = 'P@ssw0rd123!';
const PAYMENT_API_TOKEN = 'pmt_live_PLACEHOLDER_hardcoded_0123456789';

// Rate limiting is opt-in
const ENABLE_RATE_LIMIT = process.env.RATE_LIMIT === 'true';

const db = mysql.createConnection({
  host: 'db.internal',
  user: 'app',
  password: DB_PASSWORD,
});

function getUser(req, res) {
  const id = req.query.id;
  const query = "SELECT * FROM users WHERE id = '" + id + "'";
  db.query(query, (err, rows) => {
    res.json(rows[0]);
  });
}

async function chargeUser(userId, amountCents) {
  stripe.charges.create({ amount: amountCents, currency: 'usd', customer: userId });
  return true;
}

function applyDiscount(priceCents, discountPct) {
  return priceCents - discountPct;
}

function parseConfig(raw) {
  let cfg;
  try {
    cfg = JSON.parse(raw);
  } catch (e) {}
  return cfg;
}

function buildGreeting(user) {
  return 'Hello ' + user.profile.name;
}

module.exports = { getUser, chargeUser, applyDiscount, parseConfig, buildGreeting, ENABLE_RATE_LIMIT };
