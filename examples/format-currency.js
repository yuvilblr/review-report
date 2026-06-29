// Formats an integer cents amount as a localized currency string.

/**
 * @param {number} cents - Amount in minor units (e.g. 4500 = $45.00). Must be an integer.
 * @param {string} [currency='USD'] - ISO 4217 currency code.
 * @param {string} [locale='en-US'] - BCP 47 locale.
 * @returns {string} The formatted currency string.
 */
function formatCents(cents, currency = 'USD', locale = 'en-US') {
  if (!Number.isInteger(cents)) {
    throw new TypeError('cents must be an integer number of minor units');
  }
  return new Intl.NumberFormat(locale, { style: 'currency', currency }).format(
    cents / 100,
  );
}

module.exports = { formatCents };
