
CREATE TABLE public.acquisition_run (
    id bigint NOT NULL,
    marketplace_query_id bigint NOT NULL,
    status text NOT NULL,
    started_at timestamp with time zone NOT NULL,
    finished_at timestamp with time zone,
    pages integer NOT NULL,
    listings_seen integer NOT NULL,
    listings_new integer NOT NULL,
    listings_updated integer NOT NULL,
    rejected integer NOT NULL,
    error_kind text,
    error_message text,
    dry_run boolean DEFAULT false NOT NULL,
    CONSTRAINT ck_acquisition_run_error_kind CHECK ((error_kind = ANY (ARRAY['transient'::text, 'permanent'::text, 'internal'::text]))),
    CONSTRAINT ck_acquisition_run_status CHECK ((status = ANY (ARRAY['running'::text, 'succeeded'::text, 'partial'::text, 'failed'::text])))
);

ALTER TABLE public.acquisition_run ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.acquisition_run_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);

CREATE TABLE public.app_setting (
    key text NOT NULL,
    value jsonb NOT NULL
);

CREATE TABLE public.listing (
    id bigint NOT NULL,
    marketplace text NOT NULL,
    external_id text NOT NULL,
    title text,
    description text,
    price_amount numeric(12,2),
    currency character varying(3),
    price_status text,
    url text,
    images jsonb DEFAULT '[]'::jsonb NOT NULL,
    location jsonb,
    seller_external_id text,
    seller_name text,
    condition text,
    condition_raw text,
    published_at timestamp with time zone,
    first_seen_at timestamp with time zone NOT NULL,
    last_seen_at timestamp with time zone NOT NULL,
    last_seen_run_id bigint,
    availability text DEFAULT 'active'::text NOT NULL,
    origin text DEFAULT 'acquired'::text NOT NULL,
    content_hash text,
    raw_payload jsonb,
    shipping_status text DEFAULT 'unknown'::text NOT NULL,
    shipping_amount numeric(12,2),
    fees_amount numeric(12,2),
    CONSTRAINT ck_listing_acquired_has_minimum CHECK (((origin = 'legacy_seen'::text) OR ((title IS NOT NULL) AND (url IS NOT NULL) AND (price_status IS NOT NULL)))),
    CONSTRAINT ck_listing_amount_iff_priced CHECK (((price_status = 'priced'::text) = (price_amount IS NOT NULL))),
    CONSTRAINT ck_listing_amount_non_negative CHECK ((price_amount >= (0)::numeric)),
    CONSTRAINT ck_listing_availability CHECK ((availability = ANY (ARRAY['active'::text, 'not_seen'::text]))),
    CONSTRAINT ck_listing_condition CHECK ((condition = ANY (ARRAY['new'::text, 'like_new'::text, 'good'::text, 'fair'::text, 'poor'::text, 'not_applicable'::text]))),
    CONSTRAINT ck_listing_currency_with_amount CHECK (((price_amount IS NULL) OR (currency IS NOT NULL))),
    CONSTRAINT ck_listing_marketplace CHECK ((marketplace = ANY (ARRAY['fixture'::text, 'subito'::text, 'vinted'::text]))),
    CONSTRAINT ck_listing_origin CHECK ((origin = ANY (ARRAY['acquired'::text, 'legacy_seen'::text]))),
    CONSTRAINT ck_listing_price_status CHECK ((price_status = ANY (ARRAY['priced'::text, 'free'::text, 'on_request'::text, 'missing'::text]))),
    CONSTRAINT ck_listing_shipping_amount_iff_known CHECK (((shipping_status = 'known'::text) = (shipping_amount IS NOT NULL))),
    CONSTRAINT ck_listing_shipping_fees_non_negative CHECK (((shipping_amount >= (0)::numeric) AND (fees_amount >= (0)::numeric))),
    CONSTRAINT ck_listing_shipping_status CHECK ((shipping_status = ANY (ARRAY['known'::text, 'unknown'::text, 'pickup_only'::text])))
);

ALTER TABLE public.listing ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.listing_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);

CREATE TABLE public.listing_price_history (
    id bigint NOT NULL,
    listing_id bigint NOT NULL,
    price_amount numeric(12,2),
    currency character varying(3),
    price_status text NOT NULL,
    observed_at timestamp with time zone NOT NULL,
    CONSTRAINT ck_listing_price_history_amount_iff_priced CHECK (((price_status = 'priced'::text) = (price_amount IS NOT NULL))),
    CONSTRAINT ck_listing_price_history_price_status CHECK ((price_status = ANY (ARRAY['priced'::text, 'free'::text, 'on_request'::text, 'missing'::text])))
);

ALTER TABLE public.listing_price_history ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.listing_price_history_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);

CREATE TABLE public.marketplace_query (
    id bigint NOT NULL,
    search_profile_id bigint NOT NULL,
    marketplace text NOT NULL,
    params jsonb NOT NULL,
    scan_interval_minutes integer NOT NULL,
    max_pages integer NOT NULL,
    status text DEFAULT 'active'::text NOT NULL,
    last_started_at timestamp with time zone,
    baseline_completed_at timestamp with time zone,
    manual_run_requested_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_marketplace_query_marketplace CHECK ((marketplace = ANY (ARRAY['fixture'::text, 'subito'::text, 'vinted'::text]))),
    CONSTRAINT ck_marketplace_query_max_pages_min CHECK ((max_pages >= 1)),
    CONSTRAINT ck_marketplace_query_scan_interval_min CHECK ((scan_interval_minutes >= 15)),
    CONSTRAINT ck_marketplace_query_status CHECK ((status = ANY (ARRAY['active'::text, 'paused'::text])))
);

ALTER TABLE public.marketplace_query ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.marketplace_query_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);

CREATE TABLE public.match (
    id bigint NOT NULL,
    search_profile_id bigint NOT NULL,
    listing_id bigint NOT NULL,
    matched boolean NOT NULL,
    reasons jsonb NOT NULL,
    score smallint,
    score_breakdown jsonb,
    scoring_version smallint NOT NULL,
    evaluated_at timestamp with time zone NOT NULL,
    CONSTRAINT ck_match_score_only_when_matched CHECK ((matched OR (score IS NULL))),
    CONSTRAINT ck_match_score_range CHECK (((score >= 0) AND (score <= 100)))
);

ALTER TABLE public.match ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.match_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);

CREATE TABLE public.notification (
    id bigint NOT NULL,
    opportunity_id bigint NOT NULL,
    channel text NOT NULL,
    reason text NOT NULL,
    status text NOT NULL,
    suppressed_reason text,
    attempts integer DEFAULT 0 NOT NULL,
    last_error text,
    created_at timestamp with time zone NOT NULL,
    sent_at timestamp with time zone,
    CONSTRAINT ck_notification_reason_iff_suppressed CHECK (((status = 'suppressed'::text) = (suppressed_reason IS NOT NULL))),
    CONSTRAINT ck_notification_status CHECK ((status = ANY (ARRAY['pending'::text, 'sent'::text, 'failed'::text, 'suppressed'::text]))),
    CONSTRAINT ck_notification_suppressed_reason CHECK ((suppressed_reason = ANY (ARRAY['baseline'::text, 'paused'::text, 'dry_run'::text, 'legacy_seen'::text, 'shadow_mode'::text])))
);

ALTER TABLE public.notification ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.notification_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);

CREATE TABLE public.opportunity (
    id bigint NOT NULL,
    match_id bigint NOT NULL,
    status text DEFAULT 'new'::text NOT NULL,
    status_changed_at timestamp with time zone NOT NULL,
    created_at timestamp with time zone NOT NULL,
    CONSTRAINT ck_opportunity_status CHECK ((status = ANY (ARRAY['new'::text, 'seen'::text, 'dismissed'::text, 'pursued'::text, 'acquired'::text])))
);

ALTER TABLE public.opportunity ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.opportunity_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);

CREATE TABLE public.search_profile (
    id bigint NOT NULL,
    name text NOT NULL,
    keywords text[] NOT NULL,
    title_only boolean NOT NULL,
    excluded_terms text[] DEFAULT '{}'::text[] NOT NULL,
    price_min numeric(12,2),
    price_max numeric(12,2),
    price_target numeric(12,2),
    currency character varying(3) NOT NULL,
    listed_since date,
    score_threshold smallint NOT NULL,
    status text DEFAULT 'active'::text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    category text,
    CONSTRAINT ck_search_profile_has_keywords CHECK ((cardinality(keywords) > 0)),
    CONSTRAINT ck_search_profile_price_non_negative CHECK (((price_min >= (0)::numeric) AND (price_max >= (0)::numeric) AND (price_target >= (0)::numeric))),
    CONSTRAINT ck_search_profile_price_range CHECK (((price_min IS NULL) OR (price_max IS NULL) OR (price_min <= price_max))),
    CONSTRAINT ck_search_profile_score_threshold_range CHECK (((score_threshold >= 0) AND (score_threshold <= 100))),
    CONSTRAINT ck_search_profile_status CHECK ((status = ANY (ARRAY['active'::text, 'paused'::text, 'archived'::text])))
);

ALTER TABLE public.search_profile ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.search_profile_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);

ALTER TABLE ONLY public.acquisition_run
    ADD CONSTRAINT pk_acquisition_run PRIMARY KEY (id);

ALTER TABLE ONLY public.app_setting
    ADD CONSTRAINT pk_app_setting PRIMARY KEY (key);

ALTER TABLE ONLY public.listing
    ADD CONSTRAINT pk_listing PRIMARY KEY (id);

ALTER TABLE ONLY public.listing_price_history
    ADD CONSTRAINT pk_listing_price_history PRIMARY KEY (id);

ALTER TABLE ONLY public.marketplace_query
    ADD CONSTRAINT pk_marketplace_query PRIMARY KEY (id);

ALTER TABLE ONLY public.match
    ADD CONSTRAINT pk_match PRIMARY KEY (id);

ALTER TABLE ONLY public.notification
    ADD CONSTRAINT pk_notification PRIMARY KEY (id);

ALTER TABLE ONLY public.opportunity
    ADD CONSTRAINT pk_opportunity PRIMARY KEY (id);

ALTER TABLE ONLY public.search_profile
    ADD CONSTRAINT pk_search_profile PRIMARY KEY (id);

ALTER TABLE ONLY public.listing
    ADD CONSTRAINT uq_listing_marketplace_external_id UNIQUE (marketplace, external_id);

ALTER TABLE ONLY public.match
    ADD CONSTRAINT uq_match_profile_listing UNIQUE (search_profile_id, listing_id);

ALTER TABLE ONLY public.notification
    ADD CONSTRAINT uq_notification_opportunity_channel_reason UNIQUE (opportunity_id, channel, reason);

ALTER TABLE ONLY public.opportunity
    ADD CONSTRAINT uq_opportunity_match_id UNIQUE (match_id);

CREATE INDEX ix_acquisition_run_query_started_at ON public.acquisition_run USING btree (marketplace_query_id, started_at DESC);

CREATE INDEX ix_listing_last_seen_at ON public.listing USING btree (last_seen_at);

CREATE INDEX ix_listing_marketplace_id ON public.listing USING btree (marketplace, id);

CREATE INDEX ix_listing_price_history_listing_observed ON public.listing_price_history USING btree (listing_id, observed_at);

CREATE INDEX ix_marketplace_query_search_profile_id ON public.marketplace_query USING btree (search_profile_id);

CREATE INDEX ix_marketplace_query_status_last_started_at ON public.marketplace_query USING btree (status, last_started_at);

CREATE INDEX ix_match_listing_id ON public.match USING btree (listing_id);

CREATE INDEX ix_match_score ON public.match USING btree (score);

CREATE INDEX ix_notification_status ON public.notification USING btree (status);

CREATE INDEX ix_opportunity_status_created_at ON public.opportunity USING btree (status, created_at DESC);

CREATE UNIQUE INDEX uq_acquisition_run_one_running_per_query ON public.acquisition_run USING btree (marketplace_query_id) WHERE (status = 'running'::text);

ALTER TABLE ONLY public.acquisition_run
    ADD CONSTRAINT fk_acquisition_run_marketplace_query_id_marketplace_query FOREIGN KEY (marketplace_query_id) REFERENCES public.marketplace_query(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.listing
    ADD CONSTRAINT fk_listing_last_seen_run_id_acquisition_run FOREIGN KEY (last_seen_run_id) REFERENCES public.acquisition_run(id) ON DELETE SET NULL;

ALTER TABLE ONLY public.listing_price_history
    ADD CONSTRAINT fk_listing_price_history_listing_id_listing FOREIGN KEY (listing_id) REFERENCES public.listing(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.marketplace_query
    ADD CONSTRAINT fk_marketplace_query_search_profile_id_search_profile FOREIGN KEY (search_profile_id) REFERENCES public.search_profile(id) ON DELETE RESTRICT;

ALTER TABLE ONLY public.match
    ADD CONSTRAINT fk_match_listing_id_listing FOREIGN KEY (listing_id) REFERENCES public.listing(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.match
    ADD CONSTRAINT fk_match_search_profile_id_search_profile FOREIGN KEY (search_profile_id) REFERENCES public.search_profile(id) ON DELETE RESTRICT;

ALTER TABLE ONLY public.notification
    ADD CONSTRAINT fk_notification_opportunity_id_opportunity FOREIGN KEY (opportunity_id) REFERENCES public.opportunity(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.opportunity
    ADD CONSTRAINT fk_opportunity_match_id_match FOREIGN KEY (match_id) REFERENCES public.match(id) ON DELETE CASCADE;

