/**
 * The rules that catch what typechecking cannot.
 *
 * `react-hooks/rules-of-hooks` is the reason this exists. A hook placed after
 * an early return runs on some renders and not others, React tears the whole
 * tree down when the count changes, and the result is a blank window whose
 * only explanation is in a console nobody has open. It is not a type error, so
 * `tsc` is perfectly happy with it. This is a one-second static check that is
 * not.
 */
module.exports = {
  root: true,
  env: { browser: true, es2022: true },
  parser: '@typescript-eslint/parser',
  parserOptions: { ecmaVersion: 'latest', sourceType: 'module', ecmaFeatures: { jsx: true } },
  plugins: ['@typescript-eslint', 'react-hooks'],
  extends: ['eslint:recommended', 'plugin:@typescript-eslint/recommended'],
  ignorePatterns: ['dist', 'node_modules', '*.cjs'],
  rules: {
    'react-hooks/rules-of-hooks': 'error',
    'react-hooks/exhaustive-deps': 'warn',
    // Handled by tsc, and its version understands the type system.
    'no-undef': 'off',
    '@typescript-eslint/no-unused-vars': [
      'error',
      { argsIgnorePattern: '^_', varsIgnorePattern: '^_' },
    ],
    // An empty catch is how this codebase says "unsupported, and that is fine".
    'no-empty': ['error', { allowEmptyCatch: true }],
  },
}
