import { defineTool } from '@deepseek-ai/dsh-tools'

export const name = 'freqtrade-fleet-manager'
export const inject = ['tools', 'shell', 'fs', 'credentials', 'sandboxPolicy']

export function apply(ctx) {
    const shell = ctx.get('shell')
    const fs = ctx.get('fs')
    const credentials = ctx.get('credentials')
    const sandboxPolicy = ctx.get('sandboxPolicy')
    if (shell === undefined) { console.error('freqtrade: shell service unavailable; plugin disabled'); return }

    // ---- freqtrade config schema (distilled from freqtrade.config_schema.CONF_SCHEMA) ----
    const SUPPORTED_FIAT = ['AUD', 'BRL', 'CAD', 'CHF', 'CLP', 'CNY', 'CZK', 'DKK', 'EUR', 'GBP', 'HKD', 'HUF', 'IDR', 'ILS', 'INR', 'JPY', 'KRW', 'MXN', 'MYR', 'NOK', 'NZD', 'PHP', 'PKR', 'PLN', 'RUB', 'UAH', 'SEK', 'SGD', 'THB', 'TRY', 'TWD', 'ZAR', 'USD', 'BTC', 'ETH', 'XRP', 'LTC', 'BCH', 'BNB', '']
    const AVAILABLE_PAIRLISTS = ['StaticPairList', 'VolumePairList', 'PercentChangePairList', 'ProducerPairList', 'RemotePairList', 'MarketCapPairList', 'CrossMarketPairList', 'AgeFilter', 'DelistFilter', 'FullTradesFilter', 'OffsetFilter', 'PairInformationFilter', 'PerformanceFilter', 'PrecisionFilter', 'PriceFilter', 'RangeStabilityFilter', 'ShuffleFilter', 'SpreadFilter', 'VolatilityFilter']
    const ORDERTYPE_ENUM = ['limit', 'market']
    const TIF_ENUM = ['GTC', 'FOK', 'IOC', 'PO', 'gtc', 'fok', 'ioc', 'po']
    const PRICING_SIDE_ENUM = ['ask', 'bid', 'same', 'other']
    const SCHEMA = {
      type: 'object',
      properties: {
        max_open_trades: { type: ['integer', 'number'], minimum: -1 },
        timeframe: { type: 'string' },
        proxy_coin: { type: 'string' },
        stake_currency: { type: 'string' },
        stake_amount: { type: ['number', 'string'], minimum: 0.0001 },
        tradable_balance_ratio: { type: 'number', minimum: 0, maximum: 1 },
        available_capital: { type: 'number', minimum: 0 },
        amend_last_stake_amount: { type: 'boolean' },
        last_stake_amount_min_ratio: { type: 'number', minimum: 0, maximum: 1 },
        fiat_display_currency: { type: 'string', enum: SUPPORTED_FIAT },
        dry_run: { type: 'boolean' },
        dry_run_wallet: { type: ['number', 'object'] },
        cancel_open_orders_on_exit: { type: 'boolean' },
        process_only_new_candles: { type: 'boolean' },
        minimal_roi: { type: 'object' },
        amount_reserve_percent: { type: 'number', minimum: 0, maximum: 0.5 },
        stoploss: { type: 'number', maximum: 0, exclusiveMaximum: true },
        trailing_stop: { type: 'boolean' },
        trailing_stop_positive: { type: 'number', minimum: 0, maximum: 1 },
        trailing_stop_positive_offset: { type: 'number', minimum: 0, maximum: 1 },
        trailing_only_offset_is_reached: { type: 'boolean' },
        use_exit_signal: { type: 'boolean' },
        exit_profit_only: { type: 'boolean' },
        exit_profit_offset: { type: 'number' },
        recursive_strategy_search: { type: 'boolean' },
        strategy: { type: ['string', 'null'] },
        strategy_path: { type: 'string' },
        user_data_dir: { type: 'string' },
        datadir: { type: 'string' },
        fee: { type: 'number', minimum: 0, maximum: 0.1 },
        ignore_roi_if_entry_signal: { type: 'boolean' },
        ignore_buying_expired_candle_after: { type: 'number' },
        trading_mode: { type: 'string', enum: ['spot', 'margin', 'futures'] },
        margin_mode: { type: 'string', enum: ['cross', 'isolated', ''] },
        reduce_df_footprint: { type: 'boolean' },
        minimum_trade_amount: { type: 'number' },
        targeted_trade_amount: { type: 'number' },
        lookahead_analysis_exportfilename: { type: 'string' },
        startup_candle: { type: 'array' },
        liquidation_buffer: { type: 'number', minimum: 0, maximum: 0.99 },
        backtest_breakdown: { type: 'array', items: { type: 'string', enum: ['day', 'week', 'month', 'year', 'weekday'] } },
        backtest_cache: { type: 'string', enum: ['none', 'day', 'week', 'month'] },
        skip_wallet_history_migration: { type: 'boolean' },
        hyperopt_path: { type: 'string' },
        epochs: { type: 'integer', minimum: 1 },
        early_stop: { type: 'integer', minimum: 0 },
        spaces: { type: 'array', items: { type: 'string' } },
        analyze_per_epoch: { type: 'boolean' },
        print_all: { type: 'boolean' },
        hyperopt_jobs: { type: 'integer' },
        hyperopt_random_state: { type: 'integer', minimum: 0 },
        hyperopt_min_trades: { type: 'integer', minimum: 0 },
        hyperopt_loss: { type: 'string' },
        bot_name: { type: 'string' },
        unfilledtimeout: { type: 'object', properties: { entry: { type: 'number', minimum: 1 }, exit: { type: 'number', minimum: 1 }, exit_timeout_count: { type: 'number', minimum: 0 }, unit: { type: 'string', enum: ['minutes', 'seconds'] } } },
        entry_pricing: { type: 'object', required: ['price_side'], properties: { price_last_balance: { type: 'number', minimum: 0, maximum: 1 }, price_side: { type: 'string', enum: PRICING_SIDE_ENUM }, use_order_book: { type: 'boolean' }, order_book_top: { type: 'integer', minimum: 1, maximum: 50 } } },
        exit_pricing: { type: 'object', required: ['price_side'], properties: { price_last_balance: { type: 'number', minimum: 0, maximum: 1 }, price_side: { type: 'string', enum: PRICING_SIDE_ENUM }, use_order_book: { type: 'boolean' }, order_book_top: { type: 'integer', minimum: 1, maximum: 50 } } },
        custom_price_max_distance_ratio: { type: 'number', minimum: 0, maximum: 1 },
        order_types: { type: 'object', required: ['entry', 'exit', 'stoploss', 'stoploss_on_exchange'], properties: { entry: { type: 'string', enum: ORDERTYPE_ENUM }, exit: { type: 'string', enum: ORDERTYPE_ENUM }, force_exit: { type: 'string', enum: ORDERTYPE_ENUM }, force_entry: { type: 'string', enum: ORDERTYPE_ENUM }, emergency_exit: { type: 'string', enum: ORDERTYPE_ENUM }, stoploss: { type: 'string', enum: ORDERTYPE_ENUM }, stoploss_on_exchange: { type: 'boolean' }, stoploss_price_type: { type: 'string', enum: ['last', 'mark', 'index'] }, stoploss_on_exchange_interval: { type: 'number' }, stoploss_on_exchange_limit_ratio: { type: 'number', minimum: 0, maximum: 1 } } },
        order_time_in_force: { type: 'object', required: ['entry', 'exit'], properties: { entry: { type: 'string', enum: TIF_ENUM }, exit: { type: 'string', enum: TIF_ENUM } } },
        coingecko: { type: 'object', required: ['is_demo', 'api_key'], properties: { is_demo: { type: 'boolean' }, api_key: { type: 'string' } } },
        exchange: { type: 'object', required: ['name'], properties: { name: { type: 'string' }, key: { type: ['string', 'null'] }, api_key: { type: ['string', 'null'] }, secret: { type: ['string', 'null'] }, password: { type: ['string', 'null'] }, uid: { type: ['string', 'null'] }, account_id: { type: ['string', 'null'] }, wallet_address: { type: ['string', 'null'] }, private_key: { type: ['string', 'null'] }, pair_whitelist: { type: 'array', items: { type: 'string' }, uniqueItems: true }, pair_blacklist: { type: 'array', items: { type: 'string' }, uniqueItems: true }, log_responses: { type: 'boolean' }, enable_ws: { type: 'boolean' }, unknown_fee_rate: { type: 'number' }, outdated_offset: { type: 'integer', minimum: 1 }, markets_refresh_interval: { type: 'integer' } } },
        log_config: { type: 'object' },
        freqaimodel: { type: ['string', 'null'] },
        freqaimodel_path: { type: 'string' },
        freqai: { type: 'object', properties: { enabled: { type: 'boolean' }, identifier: { type: 'string' }, train_period_days: { type: 'integer' }, backtest_period_days: { type: 'number' }, live_retrain_hours: { type: 'number' }, expiration_hours: { type: 'number' }, save_backtest_models: { type: 'boolean' }, fit_live_predictions_candles: { type: 'integer' }, data_kitchen_thread_count: { type: 'integer' }, activate_tensorboard: { type: 'boolean' }, keras: { type: 'boolean' }, override_exchange_check: { type: 'boolean' } } },
        external_message_consumer: { type: 'object', required: ['producers'], properties: { enabled: { type: 'boolean' }, producers: { type: 'array', items: { type: 'object', required: ['name', 'host', 'ws_token'], properties: { name: { type: 'string' }, host: { type: 'string' }, port: { type: 'integer', minimum: 0, maximum: 65535 }, secure: { type: 'boolean' }, ws_token: { type: 'string' } } } }, wait_timeout: { type: 'integer', minimum: 0 }, sleep_time: { type: 'integer', minimum: 0 }, ping_timeout: { type: 'integer', minimum: 0 }, remove_entry_exit_signals: { type: 'boolean' }, initial_candle_limit: { type: 'integer', minimum: 0, maximum: 1500 }, message_size_limit: { type: 'integer', minimum: 1, maximum: 20 } } },
        experimental: { type: 'object' },
        pairlists: { type: 'array', minItems: 1, items: { type: 'object', required: ['method'], properties: { method: { type: 'string', enum: AVAILABLE_PAIRLISTS } } } },
        telegram: { type: 'object', required: ['enabled', 'token', 'chat_id'], properties: { enabled: { type: 'boolean' }, token: { type: 'string' }, chat_id: { type: 'string' }, topic_id: { type: 'string' }, authorized_users: { type: 'array', items: { type: 'string' } }, allow_custom_messages: { type: 'boolean' }, balance_dust_level: { type: 'number', minimum: 0 }, reload: { type: 'boolean' } } },
        webhook: { type: 'object', properties: { enabled: { type: 'boolean' }, url: { type: 'string' }, format: { type: 'string', enum: ['form', 'json', 'raw'] }, retries: { type: 'integer', minimum: 0 }, retry_delay: { type: 'number', minimum: 0 } } },
        discord: { type: 'object' },
        api_server: { type: 'object', required: ['enabled', 'listen_ip_address', 'listen_port', 'username', 'password', 'jwt_secret_key'], properties: { enabled: { type: 'boolean' }, listen_ip_address: { type: 'string', format: 'ipv4' }, listen_port: { type: 'integer', minimum: 1024, maximum: 65535 }, username: { type: 'string' }, password: { type: 'string' }, ws_token: { type: ['string', 'array'] }, jwt_secret_key: { type: 'string', minLength: 32 }, CORS_origins: { type: 'array', items: { type: 'string' } }, verbosity: { type: 'string', enum: ['error', 'info'] } } },
        db_url: { type: 'string' },
        export: { type: 'string', enum: ['none', 'trades', 'signals'] },
        disableparamexport: { type: 'boolean' },
        initial_state: { type: 'string', enum: ['running', 'paused', 'stopped'] },
        force_entry_enable: { type: 'boolean' },
        disable_dataframe_checks: { type: 'boolean' },
        internals: { type: 'object', properties: { process_throttle_secs: { type: 'integer' }, interval: { type: 'integer' }, sd_notify: { type: 'boolean' } } },
        dataformat_ohlcv: { type: 'string', enum: ['json', 'jsongz', 'feather', 'parquet'] },
        dataformat_trades: { type: 'string', enum: ['json', 'jsongz', 'feather', 'parquet'] },
        position_adjustment_enable: { type: 'boolean' },
        new_pairs_days: { type: 'integer' },
        download_trades: { type: 'boolean' },
        max_entry_position_adjustment: { type: ['integer', 'number'], minimum: -1 },
        add_config_files: { type: 'array', items: { type: 'string' } },
        orderflow: { type: 'object' }
      }
    }
    const REQUIRED = {
      other: ['exchange', 'dry_run', 'dataformat_ohlcv', 'dataformat_trades'],
      trade: ['exchange', 'timeframe', 'max_open_trades', 'stake_currency', 'stake_amount', 'tradable_balance_ratio', 'last_stake_amount_min_ratio', 'dry_run', 'dry_run_wallet', 'exit_pricing', 'entry_pricing', 'stoploss', 'minimal_roi', 'pairlists', 'internals', 'dataformat_ohlcv', 'dataformat_trades'],
      webserver: ['exchange', 'dry_run', 'dataformat_ohlcv', 'dataformat_trades', 'api_server'],
      backtest: ['exchange', 'stake_currency', 'stake_amount', 'pairlists', 'dry_run_wallet', 'dataformat_ohlcv', 'dataformat_trades', 'stoploss', 'minimal_roi', 'max_open_trades']
    }
    const KNOWN_EXTRA = ['runmode', 'timerange', 'config', 'edge', 'freqai_backtest_live_models']

    const instances = new Map()
    let registryFile = null
    let workspaceRoot = ''
    if (fs !== undefined && sandboxPolicy !== undefined && typeof sandboxPolicy.workspaceRoot === 'string' && sandboxPolicy.workspaceRoot) {
      workspaceRoot = String(sandboxPolicy.workspaceRoot).replace(/\/+$/, '')
      registryFile = workspaceRoot + '/.freqtrade-instances.json'
    }

    async function loadRegistry() {
      if (registryFile === null || fs === undefined) return
      try {
        const target = await fs.resolve(registryFile)
        const text = await fs.readText(target)
        const arr = JSON.parse(text)
        if (Array.isArray(arr)) {
          instances.clear()
          for (const it of arr) if (it && typeof it.name === 'string') instances.set(it.name, it)
        }
      } catch (e) { /* no registry yet or unreadable */ }
    }

    async function saveRegistry() {
      if (registryFile === null || fs === undefined) return
      try {
        const target = await fs.resolve(registryFile)
        await fs.writeText(target, JSON.stringify([...instances.values()], null, 2))
      } catch (e) { console.error('freqtrade: registry save failed: ' + (e && e.message)) }
    }

    function sq(s) { return "'" + String(s).replace(/'/g, "'\\''") + "'" }
    function dq(s) { return '"' + String(s).replace(/\\/g, '\\\\').replace(/"/g, '\\"').replace(/\$/g, '\\$').replace(/`/g, '\\`') + '"' }

    async function runCmd(command, timeoutMs, stdin) {
      const spec = shell.resolve({ command, timeoutMs: timeoutMs || 60000, stdoutMaxBytes: 2000000, stdin })
      const r = await shell.run(spec)
      return { exitCode: r.exitCode, stdout: (r.stdout && r.stdout.text) || '', stderr: (r.stderr && r.stderr.text) || '' }
    }

    async function curl(method, url, opts) {
      const parts = ['curl', '-sS', '-X', method, '--max-time', '40', '-w', "'\\n__FT_STATUS__%{http_code}'"]
      if (opts && opts.token) parts.push('-H', sq('Authorization: Bearer ' + opts.token))
      if (opts && opts.basic) parts.push('-u', sq(opts.basic))
      if (opts && opts.body !== undefined) parts.push('-H', sq('Content-Type: application/json'), '--data-binary', '@-')
      parts.push(sq(url))
      const spec = shell.resolve({
        command: parts.join(' '),
        timeoutMs: 45000,
        stdoutMaxBytes: 2000000,
        stdin: (opts && opts.body !== undefined) ? JSON.stringify(opts.body) : undefined
      })
      const r = await shell.run(spec)
      const raw = (r && r.stdout && r.stdout.text) || ''
      const marker = '__FT_STATUS__'
      const mi = raw.lastIndexOf(marker)
      let bodyText = raw
      let status = (r && r.exitCode === 0) ? 200 : 0
      if (mi >= 0) {
        bodyText = raw.slice(0, mi).replace(/\n+$/, '')
        const code = parseInt(raw.slice(mi + marker.length).trim(), 10)
        if (!Number.isNaN(code)) status = code
      }
      let data = bodyText
      if (bodyText) { try { data = JSON.parse(bodyText) } catch (e) { /* keep as text */ } }
      return { status, data, stderr: (r && r.stderr && r.stderr.text) || '' }
    }

    function getInst(name) {
      const inst = instances.get(name)
      if (!inst) throw new Error('unknown freqtrade instance "' + name + '" — see ft_instances_list')
      if (!inst.base_url) throw new Error('instance "' + name + '" has no base_url')
      return inst
    }

    // ---- secrets via credentials service ----
    const SECRET_KINDS = ['API_PASSWORD', 'WS_TOKEN', 'SSH_KEY', 'SSH_KEY_PASSPHRASE', 'TELEGRAM_TOKEN', 'JWT_SECRET_KEY']
    function secretRefName(name, kind) {
      return 'FTMGR_' + String(name).toUpperCase().replace(/[^A-Z0-9]/g, '_') + '_' + kind
    }

    async function resolveApiPassword(name) {
      const inst = getInst(name)
      if (credentials !== undefined && inst.password_ref) {
        const r = await credentials.resolve(inst.password_ref)
        return r ? r.value : ''
      }
      return (inst.password !== undefined && inst.password !== null) ? String(inst.password) : ''
    }

    async function secretResolve(name, kind) {
      if (kind === 'API_PASSWORD') return resolveApiPassword(name)
      if (credentials === undefined) return ''
      const r = await credentials.resolve(secretRefName(name, kind))
      return r ? r.value : ''
    }

    async function ftLogin(name) {
      const inst = getInst(name)
      if (!inst.username) throw new Error('instance "' + name + '" has no API username — set it with ft_instances_add')
      const password = await resolveApiPassword(name)
      if (!password) throw new Error('instance "' + name + '" has no API password — set it with ft_instances_add or ft_secret_set')
      const res = await curl('POST', inst.base_url.replace(/\/+$/, '') + '/api/v1/token/login', { basic: inst.username + ':' + password })
      if (res.status === 200 && res.data && res.data.access_token) return res.data.access_token
      throw new Error('login failed for "' + name + '" (HTTP ' + res.status + '): ' + JSON.stringify(res.data).slice(0, 400))
    }

    async function ftCall(name, method, path, body) {
      const inst = getInst(name)
      const url = inst.base_url.replace(/\/+$/, '') + '/api/v1' + path
      if (path === '/ping') return curl(method, url, { body })
      const token = await ftLogin(name)
      return curl(method, url, { token, body })
    }

    async function guard(fn) {
      try { return { ok: true, ...(await fn()) } }
      catch (e) { return { ok: false, error: String((e && e.message) || e) } }
    }

    function apiResult(res) {
      return { status: res.status, data: res.data, stderr: res.stderr || undefined }
    }

    function redact(inst) {
      const c = Object.assign({}, inst)
      delete c.password
      c.api_password_set = Boolean(inst.password) || Boolean(inst.password_ref)
      c.credential_ref = inst.password_ref || undefined
      return c
    }

    // ---- config validation (lightweight JSON-schema-subset against distilled schema) ----
    function typeName(v) {
      if (v === null) return 'null'
      if (Array.isArray(v)) return 'array'
      return typeof v
    }
    function matchesType(v, t) {
      if (t === 'integer') return typeof v === 'number' && Number.isInteger(v)
      if (t === 'number') return typeof v === 'number' && !Number.isNaN(v)
      if (t === 'string') return typeof v === 'string'
      if (t === 'boolean') return typeof v === 'boolean'
      if (t === 'array') return Array.isArray(v)
      if (t === 'object') return v !== null && typeof v === 'object' && !Array.isArray(v)
      if (t === 'null') return v === null
      return false
    }
    function typeMatches(v, tspec) {
      if (Array.isArray(tspec)) { for (const t of tspec) if (matchesType(v, t)) return true; return false }
      return matchesType(v, tspec)
    }
    function isIPv4(s) {
      const p = String(s).split('.')
      if (p.length !== 4) return false
      for (const x of p) {
        if (!/^\d{1,3}$/.test(x)) return false
        const n = parseInt(x, 10)
        if (n < 0 || n > 255) return false
      }
      return true
    }
    function deepEq(a, b) { return JSON.stringify(a) === JSON.stringify(b) }

    function validateNode(value, schema, path, errors) {
      if (schema === undefined || schema === null || typeof schema !== 'object') return
      if (value === undefined) return
      if (schema.type !== undefined) {
        if (!typeMatches(value, schema.type)) {
          errors.push({ path, level: 'error', message: 'expected type ' + JSON.stringify(schema.type) + ', got ' + typeName(value) })
          return
        }
      }
      if (schema.const !== undefined && !deepEq(value, schema.const)) {
        errors.push({ path, level: 'error', message: 'must equal ' + JSON.stringify(schema.const) })
      }
      if (schema.enum !== undefined) {
        let hit = false
        for (const e of schema.enum) if (deepEq(value, e)) { hit = true; break }
        if (!hit) errors.push({ path, level: 'error', message: 'must be one of: ' + JSON.stringify(schema.enum) })
      }
      if (typeof value === 'number') {
        if (schema.minimum !== undefined && value < schema.minimum) errors.push({ path, level: 'error', message: 'must be >= ' + schema.minimum })
        if (schema.maximum !== undefined) {
          const over = schema.exclusiveMaximum === true ? value >= schema.maximum : value > schema.maximum
          if (over) errors.push({ path, level: 'error', message: 'must be ' + (schema.exclusiveMaximum === true ? '<' : '<=') + ' ' + schema.maximum })
        }
      }
      if (typeof value === 'string') {
        if (schema.minLength !== undefined && value.length < schema.minLength) errors.push({ path, level: 'error', message: 'must be at least ' + schema.minLength + ' characters' })
        if (schema.format === 'ipv4' && !isIPv4(value)) errors.push({ path, level: 'error', message: 'must be a valid IPv4 address' })
      }
      if (Array.isArray(value)) {
        if (schema.minItems !== undefined && value.length < schema.minItems) errors.push({ path, level: 'error', message: 'must have at least ' + schema.minItems + ' items' })
        if (schema.maxItems !== undefined && value.length > schema.maxItems) errors.push({ path, level: 'error', message: 'must have at most ' + schema.maxItems + ' items' })
        if (schema.uniqueItems === true) {
          for (let i = 0; i < value.length; i++) {
            for (let j = i + 1; j < value.length; j++) {
              if (deepEq(value[i], value[j])) { errors.push({ path, level: 'error', message: 'items must be unique (duplicate at index ' + i + ' and ' + j + ')' }); break }
            }
          }
        }
        if (schema.items !== undefined) {
          for (let i = 0; i < value.length; i++) validateNode(value[i], schema.items, path + '[' + i + ']', errors)
        }
      }
      if (value !== null && typeof value === 'object' && !Array.isArray(value)) {
        if (schema.required !== undefined) {
          for (const req of schema.required) {
            if (!(req in value)) errors.push({ path, level: 'error', message: 'missing required field "' + req + '"' })
          }
        }
        const props = schema.properties || {}
        const patterns = schema.patternProperties || {}
        const patternKeys = Object.keys(patterns)
        for (const k in value) {
          if (Object.prototype.hasOwnProperty.call(props, k)) {
            validateNode(value[k], props[k], path + '.' + k, errors)
          } else if (patternKeys.length > 0) {
            let matched = false
            for (const pk of patternKeys) {
              try {
                if (new RegExp(pk).test(k)) { validateNode(value[k], patterns[pk], path + '.' + k, errors); matched = true; break }
              } catch (e) { /* bad regex in schema; skip */ }
            }
            if (!matched && schema.additionalProperties === false) errors.push({ path: path + '.' + k, level: 'error', message: 'unknown field "' + k + '" not allowed here' })
          } else if (schema.additionalProperties === false) {
            errors.push({ path: path + '.' + k, level: 'error', message: 'unknown field "' + k + '" not allowed here' })
          }
        }
      }
    }

    function requiredFor(runmode) {
      switch (runmode) {
        case 'dry_run': case 'live': return REQUIRED.trade
        case 'backtest': case 'hyperopt': return REQUIRED.backtest
        case 'webserver': return REQUIRED.webserver
        default: return REQUIRED.other
      }
    }

    // ---- config generation ----
    function apiPort(inst) {
      const m = /:([0-9]{1,5})(?:\/|$)/.exec(inst.base_url || '')
      return m ? parseInt(m[1], 10) : 8080
    }
    function randSecret(len) {
      let s = ''
      const chars = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789'
      for (let i = 0; i < len; i++) s += chars.charAt(Math.floor(Math.random() * chars.length))
      return s
    }
    function deepMerge(base, over) {
      if (over === undefined || over === null) return base
      if (Array.isArray(over) || Array.isArray(base)) return over
      if (typeof over === 'object' && base !== null && typeof base === 'object') {
        const out = Object.assign({}, base)
        for (const k in over) out[k] = deepMerge(base[k], over[k])
        return out
      }
      return over
    }

    async function genConfig(name, overrides) {
      const inst = getInst(name)
      const cfg = {
        dry_run: inst.dry_run !== false,
        api_server: {
          enabled: true,
          listen_ip_address: inst.host === 'ssh' ? '0.0.0.0' : '127.0.0.1',
          listen_port: apiPort(inst),
          username: inst.username || 'freqtrader',
          password: '',
          jwt_secret_key: randSecret(40)
        },
        pairlists: [{ method: 'StaticPairList' }]
      }
      if (inst.exchange) cfg.exchange = { name: inst.exchange, pair_whitelist: [] }
      if (inst.strategy) cfg.strategy = inst.strategy
      const pw = await resolveApiPassword(name)
      if (pw) cfg.api_server.password = pw
      const jwt = await secretResolve(name, 'JWT_SECRET_KEY')
      if (jwt) cfg.api_server.jwt_secret_key = jwt
      return deepMerge(cfg, overrides)
    }

    // ---- deploy / sync / bootstrap helpers ----
    function safePath(p, label) {
      const s = String(p || '').trim()
      if (!s) throw new Error(label + ' is required')
      if (s.indexOf('..') >= 0 || !/^[A-Za-z0-9._~/+-]+$/.test(s)) {
        throw new Error(label + ' may only contain letters, digits, / . _ - ~ + (no spaces, quotes, or "..")')
      }
      return s
    }
    function parseTarget(t) {
      const s = String(t || '').trim()
      const m = /^([A-Za-z0-9._-]+@)?([A-Za-z0-9._-]+?)(:([0-9]{1,5}))?$/.exec(s)
      if (!m) return null
      return { user: m[1] ? m[1].slice(0, -1) : '', host: m[2], port: m[4] ? m[4] : '' }
    }
    function targetHost(target) {
      const t = parseTarget(target)
      if (!t) throw new Error('invalid ssh_target "' + target + '" — expected user@host[:port]')
      return (t.user ? t.user + '@' : '') + t.host
    }
    function sshCmd(target, remoteCmd) {
      const t = parseTarget(target)
      if (!t) throw new Error('invalid ssh_target "' + target + '" — expected user@host[:port]')
      const args = ['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=15', '-o', 'StrictHostKeyChecking=accept-new']
      if (t.port) args.push('-p', t.port)
      args.push((t.user ? t.user + '@' : '') + t.host, dq(remoteCmd))
      return args.join(' ')
    }
    function scpCmd(target, localPath, remotePath) {
      const t = parseTarget(target)
      if (!t) throw new Error('invalid ssh_target "' + target + '"')
      const args = ['scp', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=15', '-o', 'StrictHostKeyChecking=accept-new']
      if (t.port) args.push('-P', t.port)
      args.push(sq(localPath), targetHost(target) + ':' + sq(remotePath))
      return args.join(' ')
    }
    function rsyncCmd(target, srcSpec, dstSpec, deleteExtra) {
      const t = parseTarget(target)
      if (!t) throw new Error('invalid ssh_target "' + target + '"')
      const rsh = t.port ? 'ssh -o BatchMode=yes -p ' + t.port : 'ssh -o BatchMode=yes'
      const args = ['rsync', '-az', '--info=stats1']
      if (deleteExtra) args.push('--delete')
      args.push('-e', dq(rsh), srcSpec, dstSpec)
      return args.join(' ')
    }
    function composeYaml(opts) {
      const port = opts.port || 8080
      const hostIp = opts.hostIp || '127.0.0.1'
      const strategy = opts.strategy || 'SampleStrategy'
      return [
        'services:',
        '  freqtrade:',
        '    image: ' + (opts.image || 'freqtradeorg/freqtrade:stable'),
        '    restart: unless-stopped',
        '    container_name: ' + (opts.name || 'freqtrade'),
        '    command: >',
        '      trade',
        '      --logfile /freqtrade/user_data/logs/freqtrade.log',
        '      --db-url sqlite:////freqtrade/user_data/tradesv3.sqlite',
        '      --config /freqtrade/user_data/config.json',
        '      --strategy ' + strategy,
        '    volumes:',
        '      - "./user_data:/freqtrade/user_data"',
        '    ports:',
        '      - "' + hostIp + ':' + port + ':' + port + '"',
        ''
      ].join('\n')
    }
    let composeBin = null
    async function getCompose() {
      if (composeBin !== null) return composeBin
      const r1 = await runCmd('docker compose version', 15000)
      if (r1.exitCode === 0 && /Compose version/i.test(r1.stdout + r1.stderr)) { composeBin = 'docker compose'; return composeBin }
      const r2 = await runCmd('docker-compose version', 15000)
      if (r2.exitCode === 0) { composeBin = 'docker-compose'; return composeBin }
      composeBin = 'docker compose'
      return composeBin
    }
    function composeAction(bin, action) {
      return bin + ' ' + (action === 'up' ? 'up -d' : action)
    }

    const OUT = {
      schema: { type: 'json' },
      render: (_a, v) => [{ type: 'text', text: JSON.stringify(v, null, 2) }]
    }

    function clean(v) {
      if (v === null) return null
      const t = typeof v
      if (t === 'number') return Number.isFinite(v) ? v : null
      if (t !== 'object') return v
      if (Array.isArray(v)) {
        const a = []
        for (const x of v) {
          if (x === undefined || typeof x === 'function' || typeof x === 'symbol') continue
          a.push(clean(x))
        }
        return a
      }
      const out = {}
      for (const k in v) {
        if (!Object.prototype.hasOwnProperty.call(v, k)) continue
        const val = v[k]
        if (val === undefined || typeof val === 'function' || typeof val === 'symbol') continue
        out[k] = clean(val)
      }
      return out
    }

    let toolCount = 0
    function reg(name, description, parameters, execute) {
      const wrapped = async (args) => clean(await execute(args))
      ctx.tools.register(defineTool({ name, description, parameters, output: OUT, execute: wrapped }))
      toolCount += 1
    }

    const nameParam = { name: { type: 'string', required: true, description: 'Registered instance name (see ft_instances_list).' } }
    const SECRET_ENUM = ['api_password', 'ws_token', 'ssh_key', 'ssh_key_passphrase', 'telegram_token', 'jwt_secret_key']

    // ---- registry ----
    reg('ft_instances_add', 'Register a Freqtrade instance (local or remote) for management. Requires the instance REST API base URL and credentials.', {
      name: { type: 'string', required: true, description: 'Unique short name, e.g. "binance-live".' },
      base_url: { type: 'string', required: true, description: 'REST API base URL, e.g. http://127.0.0.1:8080 or http://host:8080.' },
      api_username: { type: 'string', description: 'Freqtrade API username (required for authed endpoints).' },
      api_password: { type: 'string', description: 'Freqtrade API password. Stored in credentials (FTMGR_<NAME>_API_PASSWORD) when available, else inline; never echoed back.' },
      host: { type: 'string', enum: ['local', 'ssh'], description: '"local" (default) or "ssh" for a remote host.' },
      ssh_target: { type: 'string', description: 'For host=ssh: user@host[:port] used by deploy/sync/bootstrap.' },
      user_data: { type: 'string', description: 'Path to the instance user_data dir (local path, or remote path when host=ssh).' },
      strategy: { type: 'string', description: 'Strategy class name, e.g. SampleStrategy.' },
      exchange: { type: 'string', description: 'Exchange name, e.g. binance.' },
      dry_run: { type: 'boolean', description: 'Dry-run flag (default true).' }
    }, async (args) => {
      const name = String(args.name || '').trim()
      if (!name) return { ok: false, error: 'name is required' }
      if (!/^[a-z0-9][a-z0-9_-]{0,63}$/.test(name)) return { ok: false, error: 'name must match [a-z0-9][a-z0-9_-]{0,63}' }
      if (instances.has(name)) return { ok: false, error: 'instance "' + name + '" already exists — remove it first' }
      let password = ''
      let password_ref = ''
      if (args.api_password !== undefined && String(args.api_password) !== '') {
        if (credentials !== undefined) {
          try {
            const ref = secretRefName(name, 'API_PASSWORD')
            await credentials.set(ref, String(args.api_password))
            password_ref = ref
          } catch (e) {
            return { ok: false, error: 'failed to store api_password in credentials: ' + ((e && e.message) || e) }
          }
        } else {
          password = String(args.api_password)
        }
      }
      const inst = {
        name,
        base_url: String(args.base_url || '').trim(),
        username: args.api_username !== undefined ? String(args.api_username) : '',
        password,
        password_ref,
        host: args.host === 'ssh' ? 'ssh' : 'local',
        ssh_target: args.ssh_target ? String(args.ssh_target) : '',
        user_data: args.user_data ? String(args.user_data) : '',
        strategy: args.strategy ? String(args.strategy) : '',
        exchange: args.exchange ? String(args.exchange) : '',
        dry_run: args.dry_run !== false,
        created_at: new Date().toISOString(),
        updated_at: new Date().toISOString()
      }
      if (!inst.base_url) return { ok: false, error: 'base_url is required' }
      instances.set(name, inst)
      await saveRegistry()
      return { ok: true, registered: redact(inst), note: 'Verify connectivity with ft_ping. Secrets are never returned.' }
    })

    reg('ft_instances_list', 'List all registered Freqtrade instances (secrets redacted).', {}, async () => {
      return { ok: true, count: instances.size, instances: [...instances.values()].map(redact), registryFile }
    })

    reg('ft_instances_get', 'Show one registered instance (secrets redacted).', { name: nameParam.name }, async (args) => {
      return { ok: true, instance: redact(getInst(String(args.name))) }
    })

    reg('ft_instances_remove', 'Remove a registered instance.', { name: nameParam.name }, async (args) => {
      const name = String(args.name)
      if (!instances.has(name)) return { ok: false, error: 'no such instance: ' + name }
      instances.delete(name)
      await saveRegistry()
      return { ok: true, removed: name }
    })

    // ---- secrets (v2) ----
    reg('ft_secret_set', 'Set/rotate a secret for an instance. Stored in the credentials service (ref FTMGR_<NAME>_<KIND>); the api_password is also used for REST login.', {
      name: nameParam.name,
      secret: { type: 'string', required: true, enum: SECRET_ENUM, description: 'Secret kind.' },
      value: { type: 'string', required: true, description: 'The non-empty secret value.' }
    }, async (args) => {
      const name = String(args.name)
      const kind = String(args.secret).toUpperCase()
      if (SECRET_KINDS.indexOf(kind) < 0) return { ok: false, error: 'unknown secret kind' }
      const inst = getInst(name)
      const value = String(args.value)
      if (!value) return { ok: false, error: 'value must be non-empty' }
      try {
        if (kind === 'API_PASSWORD') {
          if (credentials !== undefined) {
            const ref = secretRefName(name, kind)
            await credentials.set(ref, value)
            inst.password_ref = ref
            inst.password = ''
          } else {
            inst.password = value
            inst.password_ref = ''
          }
          inst.updated_at = new Date().toISOString()
          await saveRegistry()
          return { ok: true, stored: credentials !== undefined ? 'credentials' : 'registry-inline', ref: inst.password_ref || undefined }
        }
        if (credentials === undefined) return { ok: false, error: 'credentials service unavailable — cannot store ' + kind.toLowerCase() }
        const ref = secretRefName(name, kind)
        await credentials.set(ref, value)
        return { ok: true, stored: 'credentials', ref }
      } catch (e) {
        return { ok: false, error: 'set failed: ' + ((e && e.message) || e) }
      }
    })

    reg('ft_secret_unset', 'Remove a secret for an instance.', {
      name: nameParam.name,
      secret: { type: 'string', required: true, enum: SECRET_ENUM, description: 'Secret kind.' }
    }, async (args) => {
      const name = String(args.name)
      const kind = String(args.secret).toUpperCase()
      if (SECRET_KINDS.indexOf(kind) < 0) return { ok: false, error: 'unknown secret kind' }
      const inst = getInst(name)
      try {
        if (kind === 'API_PASSWORD') {
          if (credentials !== undefined && inst.password_ref) await credentials.unset(inst.password_ref)
          inst.password = ''
          inst.password_ref = ''
          inst.updated_at = new Date().toISOString()
          await saveRegistry()
        } else {
          if (credentials === undefined) return { ok: false, error: 'credentials service unavailable' }
          await credentials.unset(secretRefName(name, kind))
        }
        return { ok: true, secret: kind.toLowerCase(), removed: true }
      } catch (e) {
        return { ok: false, error: 'unset failed: ' + ((e && e.message) || e) }
      }
    })

    reg('ft_secret_status', 'Report which secrets are configured for an instance (values never exposed).', { name: nameParam.name }, async (args) => {
      const name = String(args.name)
      const inst = getInst(name)
      const out = {}
      for (const kind of SECRET_KINDS) {
        const key = kind.toLowerCase()
        if (credentials !== undefined) {
          const info = await credentials.describe(secretRefName(name, kind))
          out[key] = { configured: info.configured, writable: info.writable, source: info.source || undefined, ref: secretRefName(name, kind) }
        } else if (kind === 'API_PASSWORD') {
          out[key] = { configured: Boolean(inst.password), writable: true, ref: '', inline: true }
        } else {
          out[key] = { configured: false, unavailable: true, ref: secretRefName(name, kind) }
        }
      }
      return { ok: true, name, secrets: out }
    })

    // ---- generic passthrough ----
    reg('ft_api', 'Call any Freqtrade REST API endpoint for a registered instance (auto-JWT).', {
      name: nameParam.name,
      method: { type: 'string', required: true, enum: ['GET', 'POST', 'DELETE'], description: 'HTTP method.' },
      path: { type: 'string', required: true, description: 'Path under /api/v1, e.g. "/status", "/balance", "/locks".' },
      body: { type: 'json', description: 'Optional JSON body for POST/DELETE.' }
    }, async (args) => {
      const method = String(args.method).toUpperCase()
      const path = String(args.path)
      if (!path.startsWith('/')) return { ok: false, error: 'path must start with "/"' }
      return guard(async () => apiResult(await ftCall(String(args.name), method, path, args.body)))
    })

    // ---- lifecycle + telemetry ----
    const get1 = (path) => (args) => guard(async () => apiResult(await ftCall(String(args.name), 'GET', path)))
    const post1 = (path) => (args) => guard(async () => apiResult(await ftCall(String(args.name), 'POST', path)))

    reg('ft_ping', 'Check API readiness of an instance (no auth).', { name: nameParam.name }, get1('/ping'))
    reg('ft_version', 'Show the instance version.', { name: nameParam.name }, get1('/version'))
    reg('ft_health', 'Show bot health (last bot loop).', { name: nameParam.name }, get1('/health'))
    reg('ft_sysinfo', 'Show system load information.', { name: nameParam.name }, get1('/sysinfo'))
    reg('ft_status', 'List all open trades.', { name: nameParam.name }, get1('/status'))
    reg('ft_balance', 'Show account balance per currency.', { name: nameParam.name }, get1('/balance'))
    reg('ft_profit', 'Show profit/loss summary.', { name: nameParam.name }, get1('/profit'))
    reg('ft_performance', 'Show performance of finished trades grouped by pair.', { name: nameParam.name }, get1('/performance'))
    reg('ft_whitelist', 'Show the current whitelist.', { name: nameParam.name }, get1('/whitelist'))
    reg('ft_blacklist', 'Show the current blacklist.', { name: nameParam.name }, get1('/blacklist'))
    reg('ft_locks', 'List currently locked pairs.', { name: nameParam.name }, get1('/locks'))
    reg('ft_trades', 'List recent trades (up to 500).', { name: nameParam.name }, get1('/trades'))
    reg('ft_show_config', 'Show the current operational configuration.', { name: nameParam.name }, get1('/show_config'))
    reg('ft_logs', 'Show last log messages.', { name: nameParam.name }, get1('/logs'))

    reg('ft_start', 'Start the trader.', { name: nameParam.name }, post1('/start'))
    reg('ft_stop', 'Stop the trader.', { name: nameParam.name }, post1('/stop'))
    reg('ft_pause', 'Pause the trader (gracefully handle open trades; no new entries).', { name: nameParam.name }, post1('/pause'))
    reg('ft_stopbuy', 'Stop opening new trades; gracefully close open ones.', { name: nameParam.name }, post1('/stopbuy'))
    reg('ft_reload_config', 'Reload the configuration file.', { name: nameParam.name }, post1('/reload_config'))

    // ---- mutations ----
    reg('ft_blacklist_add', 'Add one or more pairs to the blacklist.', {
      name: nameParam.name,
      pairs: { type: 'string', required: true, description: 'Comma-separated pairs, e.g. "ETH/USDT,BTC/USDT".' }
    }, async (args) => guard(async () => {
      const pairs = String(args.pairs).split(',').map((s) => s.trim()).filter(Boolean)
      return apiResult(await ftCall(String(args.name), 'POST', '/blacklist', { blacklist: pairs }))
    }))

    reg('ft_blacklist_delete', 'Remove pairs from the blacklist.', {
      name: nameParam.name,
      pairs: { type: 'string', required: true, description: 'Comma-separated pairs to remove.' }
    }, async (args) => guard(async () => {
      const pairs = String(args.pairs).split(',').map((s) => s.trim()).filter(Boolean)
      return apiResult(await ftCall(String(args.name), 'DELETE', '/blacklist', { blacklist: pairs }))
    }))

    reg('ft_forceenter', 'Force an immediate entry into a pair (force_entry_enable must be true).', {
      name: nameParam.name,
      pair: { type: 'string', required: true, description: 'Pair, e.g. BTC/USDT.' },
      side: { type: 'string', enum: ['long', 'short'], description: 'long (default) or short.' },
      price: { type: 'number', description: 'Optional limit price.' },
      stake_amount: { type: 'number', description: 'Optional stake amount.' },
      ordertype: { type: 'string', enum: ['market', 'limit'], description: 'market (default) or limit.' },
      entry_tag: { type: 'string', description: 'Optional entry tag.' }
    }, async (args) => guard(async () => {
      const body = { pair: String(args.pair) }
      if (args.side) body.side = String(args.side)
      if (args.price !== undefined) body.price = args.price
      if (args.stake_amount !== undefined) body.stakeamount = args.stake_amount
      if (args.ordertype) body.ordertype = String(args.ordertype)
      if (args.entry_tag) body.entry_tag = String(args.entry_tag)
      return apiResult(await ftCall(String(args.name), 'POST', '/forceenter', body))
    }))

    reg('ft_forceexit', 'Force an immediate exit of a trade (or all open trades).', {
      name: nameParam.name,
      tradeid: { type: 'string', required: true, description: 'Trade id, or "all" to exit every open trade.' },
      ordertype: { type: 'string', enum: ['market', 'limit'], description: 'market (default) or limit.' },
      amount: { type: 'number', description: 'Optional amount to exit (full by default).' }
    }, async (args) => guard(async () => {
      const body = { tradeid: String(args.tradeid) }
      if (args.ordertype) body.ordertype = String(args.ordertype)
      if (args.amount !== undefined) body.amount = args.amount
      return apiResult(await ftCall(String(args.name), 'POST', '/forceexit', body))
    }))

    reg('ft_lock_add', 'Lock a pair until a time (rounded up to nearest timeframe).', {
      name: nameParam.name,
      pair: { type: 'string', required: true, description: 'Pair to lock.' },
      until: { type: 'string', required: true, description: 'ISO datetime until which to lock, e.g. 2026-09-10T23:00:00.' },
      side: { type: 'string', enum: ['long', 'short'], description: 'long (default) or short.' },
      reason: { type: 'string', description: 'Optional reason.' }
    }, async (args) => guard(async () => {
      const body = { pair: String(args.pair), until: String(args.until) }
      if (args.side) body.side = String(args.side)
      if (args.reason) body.reason = String(args.reason)
      return apiResult(await ftCall(String(args.name), 'POST', '/locks', body))
    }))

    reg('ft_lock_delete', 'Delete (disable) a lock by id.', {
      name: nameParam.name,
      lock_id: { type: 'integer', required: true, description: 'Lock id from ft_locks.' }
    }, async (args) => guard(async () => {
      return apiResult(await ftCall(String(args.name), 'DELETE', '/locks/' + String(args.lock_id)))
    }))

    // ---- config (v2) ----
    reg('ft_config_validate', 'Validate a config object against the freqtrade config schema (structural lint: types, enums, required keys, ranges). Does not check exchange connectivity or strategy resolution.', {
      config: { type: 'json', required: true, description: 'The config object to validate.' },
      runmode: { type: 'string', enum: ['other', 'dry_run', 'live', 'backtest', 'hyperopt', 'webserver'], description: 'Run mode selects the required-key set (default: other, or config.runmode).' },
      strict: { type: 'boolean', description: 'Treat unknown top-level keys as errors instead of warnings (default false).' }
    }, async (args) => {
      const cfg = args.config
      if (cfg === undefined || cfg === null || typeof cfg !== 'object' || Array.isArray(cfg)) {
        return { ok: true, valid: false, errors: [{ path: '', level: 'error', message: 'config must be a JSON object' }], warnings: [] }
      }
      const runmode = args.runmode || (typeof cfg.runmode === 'string' ? cfg.runmode : 'other')
      const errors = []
      const warnings = []
      const requiredList = requiredFor(runmode)
      for (const req of requiredList) {
        if (!(req in cfg)) errors.push({ path: '', level: 'error', message: 'missing required top-level key "' + req + '"' })
      }
      for (const k in cfg) {
        if (!Object.prototype.hasOwnProperty.call(SCHEMA.properties, k) && KNOWN_EXTRA.indexOf(k) < 0) {
          const entry = { path: k, level: args.strict ? 'error' : 'warning', message: 'unknown config key "' + k + '" (freqtrade ignores it)' }
          if (args.strict) errors.push(entry); else warnings.push(entry)
        }
      }
      validateNode(cfg, SCHEMA, '', errors)
      return { ok: true, valid: errors.length === 0, runmode, required: requiredList, error_count: errors.length, warning_count: warnings.length, errors, warnings }
    })

    reg('ft_config_generate', 'Generate a starting freqtrade config.json from an instance\'s registry fields (exchange, strategy, dry_run, api_server) merged with overrides; optionally write it to a file.', {
      name: nameParam.name,
      overrides: { type: 'json', description: 'Deep-merged over the generated base config.' },
      target_path: { type: 'string', description: 'Optional path to write the config to (via the fs service).' },
      write: { type: 'boolean', description: 'Write the config to target_path (default false).' }
    }, async (args) => {
      try {
        const cfg = await genConfig(String(args.name), args.overrides)
        const result = { ok: true, name: String(args.name), config: cfg }
        if (args.write && args.target_path) {
          if (fs === undefined) return { ok: false, error: 'fs service unavailable — cannot write config file' }
          const target = await fs.resolve(safePath(args.target_path, 'target_path'))
          await fs.writeText(target, JSON.stringify(cfg, null, 2) + '\n')
          result.wrote = safePath(args.target_path, 'target_path')
        }
        return result
      } catch (e) {
        return { ok: false, error: String((e && e.message) || e) }
      }
    })

    // ---- deploy / sync / bootstrap (v2) ----
    reg('ft_deploy_local', 'Deploy a local instance as Docker Compose: write config.json + docker-compose.yml into deploy_root, then run docker compose. Dry-run (default true) only returns the files and the command it would run.', {
      name: nameParam.name,
      deploy_root: { type: 'string', description: 'Deploy directory (default: parent of the instance user_data).' },
      action: { type: 'string', enum: ['up', 'down', 'restart', 'ps'], description: 'compose action (default up).' },
      config: { type: 'json', description: 'Optional full config object (default: generated from registry).' },
      image: { type: 'string', description: 'Docker image (default freqtradeorg/freqtrade:stable).' },
      dry_run: { type: 'boolean', description: 'Return the plan without writing or executing (default true).' }
    }, async (args) => {
      try {
        const name = String(args.name)
        const inst = getInst(name)
        if (inst.host === 'ssh') return { ok: false, error: 'instance "' + name + '" is remote — use ft_deploy_ssh' }
        const dry = args.dry_run !== false
        const action = ['down', 'restart', 'ps'].indexOf(args.action) >= 0 ? args.action : 'up'
        let root = args.deploy_root ? safePath(args.deploy_root, 'deploy_root') : ''
        if (!root) {
          if (!inst.user_data) return { ok: false, error: 'deploy_root is required (instance has no user_data to derive it from)' }
          root = String(inst.user_data).replace(/\/+$/, '').replace(/\/[^/]+$/, '')
          if (!root) root = '/'
        }
        const config = args.config !== undefined ? args.config : await genConfig(name, undefined)
        const port = (config && config.api_server && config.api_server.listen_port) || apiPort(inst)
        const compose = composeYaml({ name, port, hostIp: '127.0.0.1', strategy: inst.strategy || 'SampleStrategy', image: args.image })
        const files = [
          { path: root + '/user_data/config.json', content: JSON.stringify(config, null, 2) + '\n' },
          { path: root + '/docker-compose.yml', content: compose }
        ]
        const bin = await getCompose()
        const cmd = 'cd ' + sq(root) + ' && ' + composeAction(bin, action)
        if (dry) return { ok: true, dry_run: true, deploy_root: root, action, files: files.map((f) => ({ path: f.path, bytes: f.content.length })), would_run: cmd }
        await runCmd('mkdir -p ' + sq(root + '/user_data/logs') + ' ' + sq(root + '/user_data/strategies'), 30000)
        for (const f of files) {
          const target = await fs.resolve(f.path)
          await fs.writeText(target, f.content)
        }
        const r = await runCmd(cmd, 180000)
        return { ok: r.exitCode === 0, action, deploy_root: root, exitCode: r.exitCode, wrote: files.map((f) => f.path), stdout: r.stdout, stderr: r.stderr }
      } catch (e) {
        return { ok: false, error: String((e && e.message) || e) }
      }
    })

    reg('ft_deploy_ssh', 'Deploy a remote (host=ssh) instance as Docker Compose over SSH: scp config.json + docker-compose.yml to deploy_root, then run docker compose. Dry-run (default true) returns the plan only.', {
      name: nameParam.name,
      deploy_root: { type: 'string', description: 'Remote deploy directory (default: parent of the instance user_data).' },
      action: { type: 'string', enum: ['up', 'down', 'restart', 'ps'], description: 'compose action (default up).' },
      config: { type: 'json', description: 'Optional full config object (default: generated from registry).' },
      image: { type: 'string', description: 'Docker image (default freqtradeorg/freqtrade:stable).' },
      dry_run: { type: 'boolean', description: 'Return the plan without scp/ssh (default true).' }
    }, async (args) => {
      try {
        const name = String(args.name)
        const inst = getInst(name)
        if (inst.host !== 'ssh' || !inst.ssh_target) return { ok: false, error: 'instance "' + name + '" is not a remote SSH instance (set host=ssh and ssh_target)' }
        const dry = args.dry_run !== false
        const action = ['down', 'restart', 'ps'].indexOf(args.action) >= 0 ? args.action : 'up'
        let root = args.deploy_root ? safePath(args.deploy_root, 'deploy_root') : ''
        if (!root) {
          if (!inst.user_data) return { ok: false, error: 'deploy_root is required for remote deploy' }
          root = String(inst.user_data).replace(/\/+$/, '').replace(/\/[^/]+$/, '')
        }
        if (!root) return { ok: false, error: 'deploy_root is required for remote deploy' }
        const config = args.config !== undefined ? args.config : await genConfig(name, undefined)
        const port = (config && config.api_server && config.api_server.listen_port) || apiPort(inst)
        const compose = composeYaml({ name, port, hostIp: '0.0.0.0', strategy: inst.strategy || 'SampleStrategy', image: args.image })
        const stageDir = (workspaceRoot || '/tmp') + '/.freqtrade-stage/' + name
        const localConfig = stageDir + '/config.json'
        const localCompose = stageDir + '/docker-compose.yml'
        const remoteConfig = root + '/user_data/config.json'
        const remoteCompose = root + '/docker-compose.yml'
        const mkdirCmd = sshCmd(inst.ssh_target, 'mkdir -p ' + sq(root + '/user_data/logs') + ' ' + sq(root + '/user_data/strategies'))
        const scpConfig = scpCmd(inst.ssh_target, localConfig, remoteConfig)
        const scpCompose = scpCmd(inst.ssh_target, localCompose, remoteCompose)
        const composeCmd = sshCmd(inst.ssh_target, 'cd ' + sq(root) + ' && ' + composeAction('docker compose', action))
        const plan = {
          dry_run: true,
          ssh_target: inst.ssh_target,
          deploy_root: root,
          action,
          config_bytes: (JSON.stringify(config, null, 2) + '\n').length,
          compose_bytes: compose.length,
          commands: [mkdirCmd, scpConfig, scpCompose, composeCmd]
        }
        if (dry) return { ok: true, ...plan }
        await runCmd('mkdir -p ' + sq(stageDir), 30000)
        await fs.writeText(await fs.resolve(localConfig), JSON.stringify(config, null, 2) + '\n')
        await fs.writeText(await fs.resolve(localCompose), compose)
        const mk = await runCmd(mkdirCmd, 60000)
        if (mk.exitCode !== 0) return { ok: false, step: 'mkdir', exitCode: mk.exitCode, stderr: mk.stderr }
        const s1 = await runCmd(scpConfig, 120000)
        if (s1.exitCode !== 0) return { ok: false, step: 'scp config.json', exitCode: s1.exitCode, stderr: s1.stderr }
        const s2 = await runCmd(scpCompose, 120000)
        if (s2.exitCode !== 0) return { ok: false, step: 'scp docker-compose.yml', exitCode: s2.exitCode, stderr: s2.stderr }
        const c = await runCmd(composeCmd, 180000)
        return { ok: c.exitCode === 0, action, deploy_root: root, exitCode: c.exitCode, stdout: c.stdout, stderr: c.stderr }
      } catch (e) {
        return { ok: false, error: String((e && e.message) || e) }
      }
    })

    reg('ft_sync_user_data', 'Sync the user_data directory between local and a remote (host=ssh) instance via rsync. push = local→remote, pull = remote→local. Dry-run (default true) returns the command only.', {
      name: nameParam.name,
      direction: { type: 'string', enum: ['push', 'pull'], description: 'push (default) local→remote; pull remote→local.' },
      source_path: { type: 'string', description: 'Override source path (default: instance user_data).' },
      dest_path: { type: 'string', description: 'Override destination path (default: instance user_data).' },
      delete: { type: 'boolean', description: 'Pass --delete to remove extraneous files on the destination (default false).' },
      dry_run: { type: 'boolean', description: 'Return the rsync command without executing (default true).' }
    }, async (args) => {
      try {
        const name = String(args.name)
        const inst = getInst(name)
        if (inst.host !== 'ssh' || !inst.ssh_target) return { ok: false, error: 'sync requires a remote SSH instance (host=ssh, ssh_target)' }
        if (!inst.user_data) return { ok: false, error: 'instance has no user_data path' }
        const dry = args.dry_run !== false
        const direction = args.direction === 'pull' ? 'pull' : 'push'
        const local = safePath(args.source_path || inst.user_data, 'local path').replace(/\/+$/, '')
        const remote = safePath(args.dest_path || inst.user_data, 'remote path').replace(/\/+$/, '')
        const host = targetHost(inst.ssh_target)
        const srcSpec = direction === 'push' ? sq(local + '/') : host + ':' + sq(remote + '/')
        const dstSpec = direction === 'push' ? host + ':' + sq(remote + '/') : sq(local + '/')
        const cmd = rsyncCmd(inst.ssh_target, srcSpec, dstSpec, args.delete === true)
        if (dry) return { ok: true, dry_run: true, direction, local, remote, ssh_target: inst.ssh_target, would_run: cmd }
        const r = await runCmd(cmd, 300000)
        return { ok: r.exitCode === 0, direction, exitCode: r.exitCode, stdout: r.stdout, stderr: r.stderr }
      } catch (e) {
        return { ok: false, error: String((e && e.message) || e) }
      }
    })

    reg('ft_bootstrap_host', 'Bootstrap a remote (host=ssh) host for freqtrade: install Docker (or a bare git+venv checkout), then create the user_data directory tree. Dry-run (default true) returns the script only.', {
      name: nameParam.name,
      method: { type: 'string', enum: ['docker', 'bare'], description: 'docker (default) installs Docker Engine; bare does git clone + python venv + pip install.' },
      install_root: { type: 'string', description: 'Remote install root (default: parent of the instance user_data).' },
      dry_run: { type: 'boolean', description: 'Return the bootstrap script without executing (default true).' }
    }, async (args) => {
      try {
        const name = String(args.name)
        const inst = getInst(name)
        if (inst.host !== 'ssh' || !inst.ssh_target) return { ok: false, error: 'bootstrap requires a remote SSH instance (host=ssh, ssh_target)' }
        const method = args.method === 'bare' ? 'bare' : 'docker'
        const dry = args.dry_run !== false
        let root = args.install_root ? safePath(args.install_root, 'install_root') : ''
        if (!root) {
          if (!inst.user_data) return { ok: false, error: 'install_root is required (instance has no user_data)' }
          root = String(inst.user_data).replace(/\/+$/, '').replace(/\/[^/]+$/, '')
        }
        if (!root) return { ok: false, error: 'install_root is required' }
        const rq = sq(root)
        const script = method === 'docker'
          ? [
              'set -e',
              'if ! command -v docker >/dev/null 2>&1; then echo "[ftmgr] installing docker engine"; curl -fsSL https://get.docker.com | sh; fi',
              'mkdir -p ' + rq + '/user_data/logs ' + rq + '/user_data/strategies',
              'echo "[ftmgr] bootstrap complete (docker)"'
            ].join('\n') + '\n'
          : [
              'set -e',
              'command -v python3 >/dev/null 2>&1 || { echo "python3 required"; exit 1; }',
              'command -v git >/dev/null 2>&1 || { echo "git required"; exit 1; }',
              'mkdir -p ' + rq,
              'cd ' + rq,
              'if [ ! -d freqtrade/.git ]; then git clone https://github.com/freqtrade/freqtrade.git; fi',
              'cd freqtrade && git checkout develop',
              'python3 -m venv .venv && . .venv/bin/activate && python3 -m pip install --upgrade pip && pip install -e .',
              'mkdir -p ' + rq + '/user_data/logs ' + rq + '/user_data/strategies',
              'echo "[ftmgr] bootstrap complete (bare; install TA-Lib first if backtesting)"'
            ].join('\n') + '\n'
        const cmd = sshCmd(inst.ssh_target, 'bash -s')
        if (dry) return { ok: true, dry_run: true, method, install_root: root, ssh_target: inst.ssh_target, would_run: cmd, script }
        const r = await runCmd(cmd, 600000, script)
        return { ok: r.exitCode === 0, method, exitCode: r.exitCode, stdout: r.stdout, stderr: r.stderr }
      } catch (e) {
        return { ok: false, error: String((e && e.message) || e) }
      }
    })

// ---- v3: backtesting / background jobs / download-data / hyperopt / producer links ----
    const CANDLE_TYPES = ['spot', 'futures', 'mark', 'index', 'premiumIndex', 'funding_rate', 'open_interest']
    const HYPEROPT_SPACES = ['default', 'all', 'buy', 'sell', 'enter', 'exit', 'roi', 'stoploss', 'trailing', 'protection', 'trades']

    function compactBacktestResult(res) {
      if (!res || typeof res !== 'object') return res
      const out = { strategy: {}, strategy_comparison: res.strategy_comparison }
      const strats = res.strategy && typeof res.strategy === 'object' ? res.strategy : {}
      for (const k in strats) {
        if (!Object.prototype.hasOwnProperty.call(strats, k)) continue
        const s = strats[k] || {}
        out.strategy[k] = {
          trades: s.total_trades,
          profit_total: s.profit_total,
          profit_total_abs: s.profit_total_abs,
          winrate: s.winrate,
          cagr: s.cagr,
          sortino: s.sortino,
          sharpe: s.sharpe,
          calmar: s.calmar,
          profit_factor: s.profit_factor,
          max_drawdown: s.max_drawdown,
          backtest_start: s.backtest_start,
          backtest_end: s.backtest_end,
          timeframe: s.timeframe,
          timerange: s.timerange,
          stake_currency: s.stake_currency,
          dry_run_wallet: s.dry_run_wallet,
          max_open_trades: s.max_open_trades
        }
      }
      return out
    }

    reg('ft_backtest_start', 'Start a backtest on a running instance (POST /backtest). Only one backtest/analysis job may run at a time; OHLCV data must already be downloaded. Poll with ft_backtest_status.', {
      name: nameParam.name,
      strategy: { type: 'string', required: true, description: 'Strategy class name to backtest.' },
      enable_protections: { type: 'boolean', description: 'Enable protections during the backtest (default false).' },
      timerange: { type: 'string', description: 'Timerange, e.g. 20240101-20240601.' },
      timeframe: { type: 'string', description: 'Override strategy timeframe.' },
      timeframe_detail: { type: 'string', description: 'Detailed timeframe for intraday detail.' },
      max_open_trades: { type: 'string', description: 'Override max_open_trades (integer or "inf").' },
      stake_amount: { type: 'string', description: 'Override stake_amount (number or "unlimited").' },
      dry_run_wallet: { type: 'number', description: 'Starting dry-run wallet.' },
      backtest_cache: { type: 'string', enum: ['none', 'day', 'week', 'month'], description: 'Reuse a cached result no older than this age (default none).' },
      freqaimodel: { type: 'string', description: 'FreqAI model name.' },
      freqai_identifier: { type: 'string', description: 'FreqAI identifier (sent as freqai.identifier).' }
    }, async (args) => guard(async () => {
      const body = { strategy: String(args.strategy), enable_protections: args.enable_protections === true }
      if (args.timerange) body.timerange = String(args.timerange)
      if (args.timeframe) body.timeframe = String(args.timeframe)
      if (args.timeframe_detail) body.timeframe_detail = String(args.timeframe_detail)
      if (args.max_open_trades !== undefined && String(args.max_open_trades) !== '') body.max_open_trades = String(args.max_open_trades)
      if (args.stake_amount !== undefined && String(args.stake_amount) !== '') body.stake_amount = String(args.stake_amount)
      if (args.dry_run_wallet !== undefined) body.dry_run_wallet = args.dry_run_wallet
      if (args.backtest_cache) body.backtest_cache = String(args.backtest_cache)
      if (args.freqaimodel) body.freqaimodel = String(args.freqaimodel)
      if (args.freqai_identifier) body.freqai = { identifier: String(args.freqai_identifier) }
      return { started: apiResult(await ftCall(String(args.name), 'POST', '/backtest', body)) }
    }))

    reg('ft_backtest_status', 'Poll the running/finished backtest (GET /backtest). While running returns progress/step; when finished returns a compact per-strategy summary (pass include_result for the full payload, which can be large).', {
      name: nameParam.name,
      include_result: { type: 'boolean', description: 'Include the full backtest_result payload when finished (default false: compact summary).' }
    }, async (args) => guard(async () => {
      const res = await ftCall(String(args.name), 'GET', '/backtest')
      const data = res.data && typeof res.data === 'object' ? res.data : {}
      if (data.backtest_result && args.include_result !== true) {
        return { status: res.status, data: Object.assign({}, data, { backtest_result: compactBacktestResult(data.backtest_result), compacted: true }) }
      }
      return { status: res.status, data: res.data }
    }))

    reg('ft_backtest_abort', 'Request the running backtest to abort (GET /backtest/abort).', { name: nameParam.name }, get1('/backtest/abort'))
    reg('ft_backtest_reset', 'Reset the backtest state on the instance (DELETE /backtest), freeing cached data.', { name: nameParam.name }, (args) => guard(async () => apiResult(await ftCall(String(args.name), 'DELETE', '/backtest'))))

    reg('ft_backtest_history', 'List backtest result history entries (GET /backtest/history), read from the instance user_data/backtest_results metadata.', { name: nameParam.name }, get1('/backtest/history'))

    reg('ft_backtest_history_get', 'Load one historic backtest result (GET /backtest/history/result?filename=&strategy=). Default returns a compact per-strategy summary; pass include_result for the full payload.', {
      name: nameParam.name,
      filename: { type: 'string', required: true, description: 'Result filename (stem) from ft_backtest_history.' },
      strategy: { type: 'string', required: true, description: 'Strategy name within the result file.' },
      include_result: { type: 'boolean', description: 'Return the full result payload (default false: compact summary).' }
    }, async (args) => guard(async () => {
      const qs = '?filename=' + encodeURIComponent(String(args.filename)) + '&strategy=' + encodeURIComponent(String(args.strategy))
      const res = await ftCall(String(args.name), 'GET', '/backtest/history/result' + qs)
      const data = res.data && typeof res.data === 'object' ? res.data : {}
      if (data.backtest_result && args.include_result !== true) {
        return { status: res.status, data: Object.assign({}, data, { backtest_result: compactBacktestResult(data.backtest_result), compacted: true }) }
      }
      return { status: res.status, data: res.data }
    }))

    reg('ft_backtest_history_delete', 'Delete one backtest history entry (DELETE /backtest/history/{file}).', {
      name: nameParam.name,
      file: { type: 'string', required: true, description: 'Result file stem from ft_backtest_history.' }
    }, async (args) => guard(async () => apiResult(await ftCall(String(args.name), 'DELETE', '/backtest/history/' + encodeURIComponent(String(args.file))))))

    reg('ft_backtest_history_notes', 'Attach notes to a backtest history entry (PATCH /backtest/history/{file}).', {
      name: nameParam.name,
      file: { type: 'string', required: true, description: 'Result file stem from ft_backtest_history.' },
      strategy: { type: 'string', required: true, description: 'Strategy name the notes apply to.' },
      notes: { type: 'string', description: 'Free-form notes (default empty string).' }
    }, async (args) => guard(async () => apiResult(await ftCall(String(args.name), 'PATCH', '/backtest/history/' + encodeURIComponent(String(args.file)), { strategy: String(args.strategy), notes: args.notes !== undefined ? String(args.notes) : '' }))))

    reg('ft_background_jobs', 'List background jobs (GET /background): backtest and download_data jobs with status, running flag, progress and error.', { name: nameParam.name }, get1('/background'))

    reg('ft_background_clear', 'Delete finished background jobs (DELETE /background/clear), or one specific job with job_id (DELETE /background/{jobid}). Running jobs are never deleted by the clear form.', {
      name: nameParam.name,
      job_id: { type: 'string', description: 'Delete only this job id instead of clearing all finished jobs.' }
    }, async (args) => guard(async () => apiResult(await ftCall(String(args.name), 'DELETE', args.job_id ? '/background/' + encodeURIComponent(String(args.job_id)) : '/background/clear'))))

    reg('ft_download_data', 'Start a background OHLCV/trades data download (POST /download_data) on a running instance. Only one download may run at a time; poll with ft_background_jobs (job_id is returned). timeframes and days are mutually exclusive.', {
      name: nameParam.name,
      pairs: { type: 'array', required: true, description: 'Pairs to download, e.g. ["BTC/USDT","ETH/USDT"].' },
      timeframes: { type: 'array', description: 'Candle timeframes to download (default ["1m","5m"]). Mutually exclusive with days.' },
      days: { type: 'integer', description: 'Download the last N days instead of explicit timeframes.' },
      timerange: { type: 'string', description: 'Explicit timerange instead of days, e.g. 20240101-.' },
      erase: { type: 'boolean', description: 'Erase existing data for the pairs first (default false).' },
      download_trades: { type: 'boolean', description: 'Also download raw trades (default false).' },
      candle_types: { type: 'array', description: 'Candle types to download (default: spot).', items: { type: 'string', enum: CANDLE_TYPES } },
      prepend_data: { type: 'boolean', description: 'Allow data prepending (default false).' },
      trading_mode: { type: 'string', enum: ['spot', 'margin', 'futures'], description: 'Trading mode for the download (default: instance config).' },
      margin_mode: { type: 'string', enum: ['cross', 'isolated', ''], description: 'Margin mode (futures only).' },
      exchange: { type: 'string', description: 'Override the exchange for this download.' }
    }, async (args) => guard(async () => {
      if (args.timeframes !== undefined && args.days !== undefined) return { ok: false, error: 'timeframes and days are mutually exclusive' }
      if (args.timerange && args.days !== undefined) return { ok: false, error: 'timerange and days are mutually exclusive' }
      const body = { pairs: Array.isArray(args.pairs) ? args.pairs.map(String) : [] }
      if (!body.pairs.length) return { ok: false, error: 'pairs must contain at least one pair' }
      if (args.timeframes !== undefined) body.timeframes = Array.isArray(args.timeframes) ? args.timeframes.map(String) : undefined
      if (args.days !== undefined) body.days = args.days
      if (args.timerange) body.timerange = String(args.timerange)
      if (args.erase === true) body.erase = true
      if (args.download_trades === true) body.download_trades = true
      if (args.candle_types !== undefined) body.candle_types = Array.isArray(args.candle_types) ? args.candle_types.map(String) : undefined
      if (args.prepend_data === true) body.prepend_data = true
      if (args.trading_mode) body.trading_mode = String(args.trading_mode)
      if (args.margin_mode !== undefined && String(args.margin_mode) !== '') body.margin_mode = String(args.margin_mode)
      if (args.exchange) body.exchange = String(args.exchange)
      return { started: apiResult(await ftCall(String(args.name), 'POST', '/download_data', body)) }
    }))

    // ---- v3: hyperopt via freqtrade CLI (no REST surface) ----
    const hyperoptJobs = new Map()

    function hyperoptPaths(inst, mode) {
      const ud = String(inst.user_data || '').replace(/\/+$/, '')
      if (mode === 'docker') {
        return { ud: '/freqtrade/user_data', cfg: '/freqtrade/user_data/config.json', log: '/freqtrade/user_data/logs/hyperopt-' + inst.name + '.log', pid: '', logsDir: '/freqtrade/user_data/logs', resultsDir: '/freqtrade/user_data/hyperopt_results' }
      }
      return { ud, cfg: ud + '/config.json', log: ud + '/logs/hyperopt-' + inst.name + '.log', pid: ud + '/logs/hyperopt-' + inst.name + '.pid', logsDir: ud + '/logs', resultsDir: ud + '/hyperopt_results' }
    }

    function hyperoptParts(a, p) {
      const parts = ['freqtrade', 'hyperopt', '--config', sq(p.cfg)]
      if (a.strategy) parts.push('--strategy', sq(String(a.strategy)))
      if (a.hyperopt_loss) parts.push('--hyperopt-loss', sq(String(a.hyperopt_loss)))
      const spaces = (Array.isArray(a.spaces) && a.spaces.length) ? a.spaces.map(String) : ['default']
      parts.push('--spaces', spaces.map(sq).join(' '))
      parts.push('--epochs', String(a.epochs !== undefined ? a.epochs : 100))
      if (a.timerange) parts.push('--timerange', sq(String(a.timerange)))
      if (a.timeframe) parts.push('--timeframe', sq(String(a.timeframe)))
      if (a.job_workers !== undefined) parts.push('--job-workers', String(a.job_workers))
      if (a.min_trades !== undefined) parts.push('--min-trades', String(a.min_trades))
      if (a.random_state !== undefined) parts.push('--random-state', String(a.random_state))
      if (a.early_stop !== undefined) parts.push('--early-stop', String(a.early_stop))
      if (a.print_json === true) parts.push('--print-json')
      if (a.print_all === true) parts.push('--print-all')
      if (a.extra_args) parts.push(String(a.extra_args))
      return parts.join(' ')
    }

    reg('ft_hyperopt_start', 'Start a hyperopt run in the background via the freqtrade CLI (no REST surface): mode "local" runs the binary on this machine, "ssh" on the remote host, "docker" inside a docker container (standard ./user_data -> /freqtrade/user_data mount). Logs to <user_data>/logs/hyperopt-<name>.log; poll with ft_hyperopt_status. Requires the freqtrade image with hyperopt deps for docker mode.', {
      name: nameParam.name,
      epochs: { type: 'integer', description: 'Number of epochs (default 100).' },
      strategy: { type: 'string', description: 'Strategy class name (default: instance registry strategy).' },
      hyperopt_loss: { type: 'string', description: 'Hyperopt loss class name (IHyperOptLoss), e.g. SharpeHyperOptLoss.' },
      spaces: { type: 'array', description: 'Parameter spaces to optimize (default ["default"]).', items: { type: 'string', enum: HYPEROPT_SPACES } },
      timerange: { type: 'string', description: 'Backtesting timerange, e.g. 20240101-20240601.' },
      timeframe: { type: 'string', description: 'Override timeframe.' },
      job_workers: { type: 'integer', description: 'Concurrent hyperopt worker processes (-1 = all CPUs).' },
      min_trades: { type: 'integer', description: 'Minimum trades per evaluation (default 1).' },
      random_state: { type: 'integer', description: 'Random state for reproducibility.' },
      early_stop: { type: 'integer', description: 'Early-stop after N epochs without improvement (0 = disabled).' },
      print_json: { type: 'boolean', description: 'Also print results as JSON into the log.' },
      print_all: { type: 'boolean', description: 'Print all epochs, not only improvements.' },
      extra_args: { type: 'string', description: 'Extra raw CLI args appended verbatim.' },
      mode: { type: 'string', enum: ['local', 'ssh', 'docker'], description: 'Where to run hyperopt (default: instance host kind).' },
      container: { type: 'string', description: 'Docker container name (mode docker).' },
      freqtrade_bin: { type: 'string', description: 'freqtrade binary or wrapper path (mode local; default "freqtrade").' }
    }, async (args) => guard(async () => {
      const inst = getInst(String(args.name))
      const mode = args.mode ? String(args.mode) : (inst.host === 'ssh' ? 'ssh' : 'local')
      if (mode === 'ssh' && !inst.ssh_target) return { ok: false, error: 'instance "' + inst.name + '" has no ssh_target — set it with ft_instances_add or use mode local/docker' }
      if (mode === 'docker' && !args.container) return { ok: false, error: 'container is required for mode docker' }
      if (!inst.user_data) return { ok: false, error: 'instance has no user_data path — set it with ft_instances_add' }
      const p = hyperoptPaths(inst, mode)
      const inner = hyperoptParts(args, p) + ' > ' + sq(p.log) + ' 2>&1 & echo FTMGR_HYPEROPT_PID:$!'
      let command = ''
      let mkdirCmd = ''
      if (mode === 'docker') {
        const dockerInner = 'mkdir -p ' + sq(p.logsDir) + ' && ' + hyperoptParts(args, p) + ' > ' + sq(p.log) + ' 2>&1'
        command = 'docker exec -d ' + sq(String(args.container)) + ' bash -c ' + dq(dockerInner)
      } else if (mode === 'ssh') {
        mkdirCmd = sshCmd(inst.ssh_target, 'mkdir -p ' + sq(p.logsDir)).command
        command = sshCmd(inst.ssh_target, inner).command
      } else {
        const bin = args.freqtrade_bin ? String(args.freqtrade_bin) : 'freqtrade'
        mkdirCmd = 'mkdir -p ' + sq(p.logsDir)
        command = inner.replace(/^freqtrade /, bin + ' ')
      }
      if (mkdirCmd) {
        const mk = await runCmd(mkdirCmd, 20000)
        if (mk.exitCode !== 0) return { ok: false, error: 'failed to create logs dir (exit ' + mk.exitCode + '): ' + mk.stderr.slice(0, 500), command: mkdirCmd }
      }
      const out = await runCmd(command, 30000)
      if (out.exitCode !== 0) return { ok: false, error: 'failed to start hyperopt (exit ' + out.exitCode + ')', stderr: out.stderr.slice(0, 2000), command }
      let pid = ''
      if (mode !== 'docker') {
        const m = /FTMGR_HYPEROPT_PID:(\d+)/.exec(out.stdout)
        if (!m) return { ok: false, error: 'hyperopt started but no PID echoed', stdout: out.stdout.slice(0, 1000), command }
        pid = m[1]
        const wr = mode === 'ssh'
          ? await runCmd(sshCmd(inst.ssh_target, 'echo ' + pid + ' > ' + sq(p.pid)).command, 20000)
          : await runCmd('echo ' + pid + ' > ' + sq(p.pid), 20000)
        if (wr.exitCode !== 0) return { ok: false, error: 'hyperopt started (pid ' + pid + ') but writing the pid file failed', command }
      }
      const job = { instance: inst.name, mode, container: args.container ? String(args.container) : '', pid, log: p.log, pid_file: p.pid, results_dir: p.resultsDir, logs_dir: p.logsDir, started_at: new Date().toISOString(), command }
      hyperoptJobs.set(inst.name, job)
      return { ok: true, started: true, job }
    }))

    reg('ft_hyperopt_status', 'Check a hyperopt run started with ft_hyperopt_start: process liveness, last log lines, and the newest files in user_data/hyperopt_results.', {
      name: nameParam.name,
      tail_lines: { type: 'integer', description: 'Log lines to return (default 30).' },
      result_files: { type: 'integer', description: 'Newest hyperopt_results entries to list (default 5).' }
    }, async (args) => guard(async () => {
      const inst = getInst(String(args.name))
      const job = hyperoptJobs.get(inst.name) || {}
      const mode = job.mode || (inst.host === 'ssh' ? 'ssh' : 'local')
      const p = hyperoptPaths(inst, mode)
      const log = job.log || p.log
      const pidFile = job.pid_file || p.pid
      const resultsDir = job.results_dir || p.resultsDir
      const tail = String(args.tail_lines !== undefined ? args.tail_lines : 30)
      const files = String(args.result_files !== undefined ? args.result_files : 5)
      let probeCmd = ''
      let isDocker = false
      if (mode === 'docker') {
        isDocker = true
        const c = sq(job.container || String(args.container || ''))
        const inner = 'pgrep -af "freqtrade hyperopt" | head -3 || echo NO_PROCESS; echo ---LOG---; tail -n ' + tail + ' ' + sq(log) + '; echo ---RESULTS---; ls -t ' + sq(resultsDir) + ' 2>/dev/null | head -n ' + files
        probeCmd = 'docker exec ' + c + ' bash -c ' + dq(inner)
      } else if (mode === 'ssh') {
        const script = 'PID=$(cat ' + sq(pidFile) + ' 2>/dev/null); if [ -n "$PID" ] && ps -p "$PID" > /dev/null 2>&1; then echo RUNNING:$PID; else echo NO_PROCESS; fi; echo ---LOG---; tail -n ' + tail + ' ' + sq(log) + '; echo ---RESULTS---; ls -t ' + sq(resultsDir) + ' 2>/dev/null | head -n ' + files
        probeCmd = sshCmd(inst.ssh_target, script).command
      } else {
        probeCmd = 'PID=$(cat ' + sq(pidFile) + ' 2>/dev/null); if [ -n "$PID" ] && ps -p "$PID" > /dev/null 2>&1; then echo RUNNING:$PID; else echo NO_PROCESS; fi; echo ---LOG---; tail -n ' + tail + ' ' + sq(log) + '; echo ---RESULTS---; ls -t ' + sq(resultsDir) + ' 2>/dev/null | head -n ' + files
      }
      const out = await runCmd(probeCmd, 30000)
      const txt = out.stdout || ''
      const lines = txt.split('\n')
      const first = (lines.find((l) => l.trim() !== '') || '').trim()
      let running = false
      if (isDocker) running = first !== '' && first !== 'NO_PROCESS'
      else running = /^RUNNING:/.test(first)
      const seg = (marker) => {
        const i = txt.indexOf(marker)
        if (i < 0) return ''
        const rest = txt.slice(i + marker.length)
        const j = rest.indexOf('---')
        return (j >= 0 ? rest.slice(0, j) : rest).trim()
      }
      return {
        ok: true,
        running,
        pid: isDocker ? '' : (/^RUNNING:(\d+)$/.exec(first) || [])[1] || job.pid || '',
        first_line: isDocker ? first.slice(0, 300) : first,
        log_tail: seg('---LOG---'),
        result_files: seg('---RESULTS---').split('\n').filter(Boolean),
        mode,
        exit_code: out.exitCode,
        stderr: out.stderr || ''
      }
    }))

    reg('ft_hyperopt_stop', 'Stop a hyperopt run started with ft_hyperopt_start (kill the pid, or pkill inside the docker container).', { name: nameParam.name }, async (args) => guard(async () => {
      const inst = getInst(String(args.name))
      const job = hyperoptJobs.get(inst.name)
      const mode = (job && job.mode) || (inst.host === 'ssh' ? 'ssh' : 'local')
      let command = ''
      if (mode === 'docker') {
        const c = job && job.container ? job.container : ''
        if (!c) return { ok: false, error: 'no container recorded for this hyperopt job (started before this session?) — pass it via ft_hyperopt_start again' }
        command = 'docker exec ' + sq(c) + ' pkill -f ' + sq('freqtrade hyperopt')
      } else if (mode === 'ssh') {
        const p = hyperoptPaths(inst, mode)
        const pidFile = (job && job.pid_file) || p.pid
        command = sshCmd(inst.ssh_target, 'PID=$(cat ' + sq(pidFile) + ' 2>/dev/null); if [ -n "$PID" ] && ps -p "$PID" > /dev/null 2>&1; then kill "$PID" && echo KILLED:$PID; else echo NO_PROCESS; fi').command
      } else {
        const p = hyperoptPaths(inst, mode)
        const pidFile = (job && job.pid_file) || p.pid
        command = 'PID=$(cat ' + sq(pidFile) + ' 2>/dev/null); if [ -n "$PID" ] && ps -p "$PID" > /dev/null 2>&1; then kill "$PID" && echo KILLED:$PID; else echo NO_PROCESS; fi'
      }
      const out = await runCmd(command, 20000)
      if (job) hyperoptJobs.delete(inst.name)
      return { ok: out.exitCode === 0, stopped: true, stdout: out.stdout.trim(), stderr: out.stderr.slice(0, 1000), command }
    }))

    // ---- v3: producer/consumer topology ----
    reg('ft_instance_link_producer', 'Build (and optionally write) the external_message_consumer entry that links a consumer instance to a producer instance. Requires the producer ws_token secret (ft_secret_set ... ws_token). write=true merges the entry into the consumer config.json (local: fs; ssh: ssh) and reminds you to restart the consumer.', {
      consumer: { type: 'string', required: true, description: 'Consumer instance name (receives signals from the producer).' },
      producer: { type: 'string', required: true, description: 'Producer instance name (its REST API websocket is consumed).' },
      link_name: { type: 'string', description: 'Producer link name in the consumer config (default: producer instance name).' },
      secure: { type: 'boolean', description: 'Use wss (default false).' },
      initial_candle_limit: { type: 'integer', description: 'Initial candle load limit (max 1500).' },
      message_size_limit: { type: 'integer', description: 'Websocket message size limit in MB (1..20).' },
      remove_entry_exit_signals: { type: 'boolean', description: 'Remove entry/exit signals from producer messages.' },
      write: { type: 'boolean', description: 'Merge the entry into the consumer config.json (default false: return the entry + instructions only).' }
    }, async (args) => guard(async () => {
      const consumer = getInst(String(args.consumer))
      const producer = getInst(String(args.producer))
      if (!consumer.user_data) return { ok: false, error: 'consumer instance has no user_data path — set it with ft_instances_add' }
      const wsToken = await secretResolve(producer.name, 'WS_TOKEN')
      if (!wsToken) return { ok: false, error: 'producer "' + producer.name + '" has no ws_token secret — set it with ft_secret_set(name, "ws_token", value) first' }
      const pu = String(producer.base_url || '').replace(/^https?:\/\//, '').replace(/\/+$/, '')
      const slash = pu.indexOf('/')
      const hostPort = slash >= 0 ? pu.slice(0, slash) : pu
      const cIdx = hostPort.lastIndexOf(':')
      const host = cIdx > 0 ? hostPort.slice(0, cIdx) : hostPort
      const port = cIdx > 0 ? parseInt(hostPort.slice(cIdx + 1), 10) : (String(producer.base_url).indexOf('https://') === 0 ? 443 : 80)
      const entry = { name: args.link_name ? String(args.link_name) : producer.name, host, port }
      if (args.secure === true) entry.secure = true
      entry.ws_token = wsToken
      if (args.initial_candle_limit !== undefined) entry.initial_candle_limit = args.initial_candle_limit
      if (args.message_size_limit !== undefined) entry.message_size_limit = args.message_size_limit
      if (args.remove_entry_exit_signals !== undefined) entry.remove_entry_exit_signals = args.remove_entry_exit_signals === true
      const redacted = Object.assign({}, entry, { ws_token: '***' })
      const consumerCfg = String(consumer.user_data).replace(/\/+$/, '') + '/config.json'
      const refPlaceholder = '<FTMGR_' + String(producer.name).toUpperCase().replace(/[^A-Z0-9]/g, '_') + '_WS_TOKEN>'
      if (args.write !== true) {
        return {
          ok: true,
          entry: redacted,
          consumer_config: consumerCfg,
          note: 'Merge into "external_message_consumer": { "enabled": true, "producers": [ <entry> ] } in the consumer config, then restart the consumer (ft_stop + ft_start or docker restart).',
          json_snippet: JSON.stringify({ enabled: true, producers: [Object.assign({}, entry, { ws_token: refPlaceholder })] }, null, 2)
        }
      }
      let cfgText = ''
      if (consumer.host === 'ssh' && consumer.ssh_target) {
        const r = await runCmd(sshCmd(consumer.ssh_target, 'cat ' + sq(consumerCfg)).command, 30000)
        if (r.exitCode !== 0) return { ok: false, error: 'failed to read consumer config over ssh: ' + (r.stderr || '').slice(0, 500) }
        cfgText = r.stdout
      } else if (fs !== undefined) {
        const target = await fs.resolve(consumerCfg)
        cfgText = await fs.readText(target)
      } else {
        return { ok: false, error: 'consumer is local but the fs service is unavailable' }
      }
      let cfg
      try { cfg = JSON.parse(cfgText) } catch (e) { return { ok: false, error: 'consumer config.json is not valid JSON: ' + e.message } }
      const emc = cfg.external_message_consumer && typeof cfg.external_message_consumer === 'object' ? cfg.external_message_consumer : {}
      const producers = Array.isArray(emc.producers) ? emc.producers.filter((x) => x && x.name !== entry.name) : []
      producers.push(entry)
      cfg.external_message_consumer = Object.assign({}, emc, { enabled: emc.enabled !== false, producers })
      const nextText = JSON.stringify(cfg, null, 2) + '\n'
      if (consumer.host === 'ssh' && consumer.ssh_target) {
        const r = await runCmd(sshCmd(consumer.ssh_target, 'cat > ' + sq(consumerCfg)).command, 30000, nextText)
        if (r.exitCode !== 0) return { ok: false, error: 'failed to write consumer config over ssh: ' + (r.stderr || '').slice(0, 500) }
      } else if (fs !== undefined) {
        const target = await fs.resolve(consumerCfg)
        await fs.writeText(target, nextText)
      }
      return {
        ok: true,
        written: true,
        consumer_config: consumerCfg,
        entry: redacted,
        producers_now: producers.map((x) => x && x.name),
        note: 'Restart the consumer to apply: ft_stop + ft_start, or docker restart for container deploys.'
      }
    }))
    loadRegistry()
    console.log('freqtrade-fleet-manager: apply() registered ' + toolCount + ' tools; registry file: ' + (registryFile || 'none (in-memory only)'))

  // ---- dashboard RPC: GET /freqtrade/api/fleet (served by the host web server) ----
  ctx.inject(['webServer'], (webCtx) => {
    const webServer = webCtx.get('webServer')
    if (webServer === undefined) return () => {}
    webCtx.effect(() => {
      try {
        return webServer.register({
          kind: 'prefix',
          path: '/freqtrade',
          handler: async (req, res) => {
            const url = new URL(req.url || '/freqtrade', 'http://localhost')
            if (url.pathname === '/freqtrade/api/fleet' && req.method === 'GET') {
              const wantPing = url.searchParams.get('ping') === '1'
              const list = [...instances.values()].map((it) => {
                const c = redact(it)
                return {
                  name: c.name, base_url: c.base_url, host: c.host, strategy: c.strategy,
                  exchange: c.exchange, dry_run: c.dry_run, api_password_set: c.api_password_set,
                  ssh_target: c.ssh_target, user_data: c.user_data, updated_at: c.updated_at
                }
              })
              const health = []
              if (wantPing) {
                for (const it of list) {
                  let up = null
                  let latency_ms = null
                  if (it.base_url && shell !== undefined) {
                    const u = String(it.base_url).replace(/\/+$/, '') + '/api/v1/ping'
                    const started = Date.now()
                    const r = await runCmd('curl -sS --max-time 6 -o /dev/null -w ' + sq('%{http_code}') + ' ' + sq(u), 9000)
                    latency_ms = Date.now() - started
                    const code = String((r && r.stdout) || '').trim()
                    up = r.exitCode === 0 && /^2/.test(code)
                  }
                  health.push({ name: it.name, up, latency_ms })
                }
              }
              res.writeHead(200, { 'Content-Type': 'application/json' })
              res.end(JSON.stringify({ count: list.length, registryFile, instances: list, health }))
              return
            }
            res.writeHead(404, { 'Content-Type': 'application/json' })
            res.end(JSON.stringify({ ok: false, error: 'not found' }))
          }
        })
      } catch (e) {
        console.error('freqtrade-fleet-manager: webServer.register(/freqtrade) failed: ' + (e && e.message))
        return () => {}
      }
    }, 'freqtrade-fleet-manager: webServer.register(/freqtrade)')
    return () => {}
  })
}
