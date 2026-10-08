"""The monitoring dashboard (ADR-0023): the batch's KPIs and its alert queue.

Thin on purpose (CLAUDE.md, rule 4): every number comes from `atalayero.monitoring.kpis` and
`atalayero.batch.queries`, which read the serving database the batch publishes, read-only. This
file only lays them out. Run it with `make dashboard`, or `make up` for the container.
"""

import altair as alt
import pandas as pd
import streamlit as st

from atalayero.batch import queries
from atalayero.monitoring import kpis
from atalayero.settings import Settings

# The first three slots of the reference categorical palette (the dataviz skill's palette.md),
# in fixed order: validated as a set in both modes. Text never wears them.
SERIES = {
    "light": ["#2a78d6", "#eb6834", "#1baf7a"],
    "dark": ["#3987e5", "#d95926", "#199e70"],
}
PHASES = ["train", "validation", "test"]  # the warm-up day has no queue

st.set_page_config(page_title="Atalayero", page_icon=":material/monitoring:", layout="wide")
settings = Settings()
colors = SERIES["dark" if st.context.theme.type == "dark" else "light"]


@st.cache_data(ttl=60, show_spinner=False)
def load(serving: str, phases: tuple[str, ...]) -> dict[str, pd.DataFrame]:
    """The KPI tables of the chosen phases, from `serving`; cached a minute, as the batch may
    publish again."""
    return {
        "daily": kpis.daily_kpis(settings, phases),
        "rules": kpis.rule_kpis(settings, phases),
        "typologies": kpis.typology_kpis(settings, phases),
        "drift": kpis.drift_days(settings),
    }


st.title("Atalayero: transaction monitoring")
st.caption(
    "Synthetic data only (IBM AML, HI-Small): these numbers describe a simulation, not "
    "performance on real transactions. False positives and detection use the dataset's labels, "
    "in hindsight; train days were scored by a model that saw them."
)

phases = st.segmented_control(
    "Days", PHASES, default=["test"], selection_mode="multi", help="Phases of the simulation"
)
if not phases:
    st.info("Choose at least one phase.")
    st.stop()
try:
    data = load(str(settings.batch.serving_path), tuple(phases))
except kpis.ServingUnavailableError:
    st.info("No data published yet: run `make replay` (or the `daily_batch` DAG).")
    st.stop()
daily = data["daily"]
if daily.empty:
    st.info("No complete day in these phases yet.")
    st.stop()


def share(value: float | None) -> str:
    return "—" if value is None else f"{value:.0%}"


headline = kpis.summary(daily)
tiles = st.columns(4)
tiles[0].metric("Days", headline["days"])
tiles[1].metric("Alerts a day", f"{headline['alerts_per_day']:.0f}")
tiles[2].metric(
    "Alerts with no laundering",
    share(headline["false_positive_share"]),
    help="Account-days of the queue that hold no laundering transaction.",
)
tiles[3].metric(
    "Laundering detected",
    share(headline["detection_rate"]),
    help="Laundering transactions on an account-day of the queue.",
)

left, right = st.columns(2)
with left:
    st.subheader("The queue, by source")
    sources = kpis.queue_sources(daily).replace(
        {"model_only": "Model only", "rules_only": "Rules only", "both": "Both"}
    )
    st.altair_chart(
        alt.Chart(sources)
        .mark_bar(cornerRadiusTopLeft=4, cornerRadiusTopRight=4, stroke=None)
        .encode(
            x=alt.X("day:T", title=None, axis=alt.Axis(format="%d %b", grid=False)),
            y=alt.Y("alerts:Q", title="Account-days alerted", stack="zero"),
            color=alt.Color(
                "source:N",
                title="Source",
                scale=alt.Scale(domain=["Model only", "Rules only", "Both"], range=colors),
                legend=alt.Legend(orient="top"),
            ),
            order=alt.Order("source:N"),
            tooltip=[
                alt.Tooltip("day:T", format="%d %b %Y"),
                "phase:N",
                "source:N",
                "alerts:Q",
            ],
        ),
    )
with right:
    st.subheader("Detection and false positives, by day")
    shares = daily.melt(
        id_vars=["day", "phase"],
        value_vars=["detection_rate", "false_positive_share"],
        var_name="measure",
        value_name="share",
    ).replace(
        {
            "detection_rate": "Laundering detected",
            "false_positive_share": "Alerts with no laundering",
        }
    )
    st.altair_chart(
        alt.Chart(shares)
        .mark_line(point=alt.OverlayMarkDef(size=64), strokeWidth=2)
        .encode(
            x=alt.X("day:T", title=None, axis=alt.Axis(format="%d %b", grid=False)),
            y=alt.Y(
                "share:Q", title="Share", axis=alt.Axis(format="%"), scale=alt.Scale(domain=[0, 1])
            ),
            color=alt.Color(
                "measure:N",
                title=None,
                scale=alt.Scale(
                    domain=["Laundering detected", "Alerts with no laundering"], range=colors[:2]
                ),
                legend=alt.Legend(orient="top"),
            ),
            tooltip=[
                alt.Tooltip("day:T", format="%d %b %Y"),
                "phase:N",
                "measure:N",
                alt.Tooltip("share:Q", format=".1%"),
            ],
        ),
    )

left, right = st.columns(2)
with left:
    st.subheader("Rules")
    rules = kpis.rule_totals(data["rules"])
    st.dataframe(
        rules[["rule_id", "alerts", "account_days", "hit_rate", "in_queue"]],
        hide_index=True,
        column_config={
            "rule_id": "Rule",
            "alerts": "Alerts",
            "account_days": "Account-days",
            "hit_rate": st.column_config.NumberColumn("Hold laundering", format="percent"),
            "in_queue": st.column_config.NumberColumn(
                "Reach the queue", format="percent", help="The rest fall on hub accounts."
            ),
        },
    )
with right:
    st.subheader("Laundering detected, by typology")
    typologies = kpis.typology_totals(data["typologies"])
    st.altair_chart(
        alt.Chart(typologies)
        .mark_bar(cornerRadiusTopRight=4, cornerRadiusBottomRight=4, color=colors[0])
        .encode(
            x=alt.X(
                "detection_rate:Q",
                title="Detected",
                axis=alt.Axis(format="%"),
                scale=alt.Scale(domain=[0, 1]),
            ),
            y=alt.Y("typology:N", title=None, sort="-x"),
            tooltip=[
                "typology:N",
                alt.Tooltip("detection_rate:Q", title="Detected", format=".1%"),
                alt.Tooltip("laundering_transactions:Q", title="Laundering transactions"),
            ],
        ),
    )

st.subheader("Drift against train")
drift = data["drift"]
drift = drift[drift["phase"].isin(phases)]
if drift.empty:
    st.caption("Drift is checked on validation and test days.")
else:
    st.dataframe(
        drift.assign(
            top=drift["top_features"].map(
                lambda fs: ", ".join(f"{f['feature']} {f['psi']:.2f}" for f in fs)
            ),
            reasons=drift["reasons"].map(lambda r: "; ".join(r) or "—"),
        )[
            [
                "day",
                "phase",
                "drift_detected",
                "significant",
                "moderate",
                "rule_alerts",
                "top",
                "reasons",
            ]
        ],
        hide_index=True,
        column_config={
            "day": st.column_config.DateColumn("Day", format="D MMM YYYY"),
            "drift_detected": "Drift",
            "significant": "Features ≥ 0.25",
            "moderate": "Features ≥ 0.1",
            "rule_alerts": "Rule alerts",
            "top": "Most drifted (PSI)",
            "reasons": "Why",
        },
    )

st.subheader("The alert queue")
day = st.selectbox(
    "Day", list(daily["day"].dt.date)[::-1], format_func=lambda d: d.strftime("%d %b %Y")
)
page = queries.list_alerts(settings, day=day, limit=settings.api.page_limit)
queue = pd.DataFrame(
    [
        {
            "alert_id": a.alert_id,
            "rank": a.rank,
            "score": a.score,
            "sources": ", ".join(a.sources),
            "transactions": len(a.transaction_ids),
        }
        for a in page.alerts
    ]
)
picked = st.dataframe(
    queue,
    hide_index=True,
    on_select="rerun",
    selection_mode="single-row",
    column_config={
        "alert_id": "Alert",
        "rank": "Rank",
        "score": st.column_config.NumberColumn("Score", format="%.3f"),
        "sources": "Sources",
        "transactions": "Raised by",
    },
)
rows = picked.selection.rows
if rows:
    case = queries.get_case(settings, queue.iloc[rows[0]]["alert_id"])
    if case is not None:
        st.markdown(f"**{case.alert.alert_id}** · {case.alert.phase} day · rank {case.alert.rank}")
        st.dataframe(
            pd.DataFrame([t.model_dump() for t in case.transactions])[
                [
                    "transacted_at",
                    "direction",
                    "sender_account_key",
                    "receiver_account_key",
                    "amount_paid_usd",
                    "payment_format",
                    "score",
                    "raised_alert",
                ]
            ],
            hide_index=True,
            column_config={
                "transacted_at": st.column_config.DatetimeColumn("When", format="HH:mm"),
                "amount_paid_usd": st.column_config.NumberColumn("USD", format="%.2f"),
                "score": st.column_config.NumberColumn("Score", format="%.3f"),
                "raised_alert": "Raised it",
            },
        )
        if case.investigation and case.investigation.report:
            report = case.investigation.report
            st.markdown(
                f"**The agent:** {report.decision}, {report.typology} "
                f"(grounded: {case.investigation.grounded})"
            )
            st.write(report.narrative)
        else:
            st.caption("The investigator agent has not looked at this alert.")
