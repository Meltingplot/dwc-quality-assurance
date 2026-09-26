/**
 * One ruleset: the frontend is Vue 3.5 only (DWC 3.7). TypeScript in src/, plain JS in
 * tests/ (see vitest.config.mjs for why).
 */
const VUE_RULES = {
    'vue/html-indent': ['error', 'tab'],
    'vue/max-attributes-per-line': 'off',
    'vue/singleline-html-element-content-newline': 'off',
    'vue/multi-word-component-names': 'off',
    'vue/html-self-closing': 'off',
    // attributes continue on the next line without a line break before the first (DWC/CHX350 style)
    'vue/first-attribute-linebreak': 'off',
    'vue/html-closing-bracket-newline': 'off',
};

module.exports = {
    root: true,
    env: {
        browser: true,
        es2022: true,
    },
    extends: ['eslint:recommended'],
    parserOptions: {
        ecmaVersion: 2022,
        sourceType: 'module',
    },
    rules: {
        'no-console': 'warn',
        'no-unused-vars': ['error', { argsIgnorePattern: '^_' }],
    },
    overrides: [
        {
            files: ['src/**/*.ts'],
            parser: '@typescript-eslint/parser',
            plugins: ['@typescript-eslint'],
            extends: ['eslint:recommended', 'plugin:@typescript-eslint/recommended'],
            rules: {
                'no-console': 'warn',
                'no-unused-vars': 'off',
                '@typescript-eslint/no-unused-vars': ['error', { argsIgnorePattern: '^_' }],
                '@typescript-eslint/no-explicit-any': 'off',
            },
        },
        {
            files: ['src/**/*.vue'],
            parser: 'vue-eslint-parser',
            parserOptions: {
                parser: '@typescript-eslint/parser',
                ecmaVersion: 2022,
                sourceType: 'module',
            },
            plugins: ['@typescript-eslint'],
            extends: ['plugin:vue/vue3-recommended'],
            rules: {
                ...VUE_RULES,
                'no-unused-vars': 'off',
                '@typescript-eslint/no-unused-vars': ['error', { argsIgnorePattern: '^_' }],
            },
        },
        {
            files: ['tests/**/*.js'],
            env: { node: true },
        },
    ],
};
