from django import forms
from django.core.exceptions import ValidationError

from apps.leagues.models import League
from apps.leagues.services import LeagueLookupError, lookup_league


class LeagueForm(forms.ModelForm):
    is_private = forms.BooleanField(required=False)

    class Meta:
        model = League
        fields = ["platform", "league_id", "name", "league_url", "espn_s2", "swid"]

    def __init__(self, *args, user=None, **kwargs):
        self.user = user
        super().__init__(*args, **kwargs)
        self.fields["name"].required = False
        self.fields["league_url"].required = False
        self.fields["espn_s2"].required = False
        self.fields["swid"].required = False

    def clean(self):
        cleaned = super().clean()
        platform = cleaned.get("platform")
        league_id = (cleaned.get("league_id") or "").strip()
        cleaned["league_id"] = league_id
        cleaned["league_url"] = (cleaned.get("league_url") or "").strip()
        cleaned["espn_s2"] = (cleaned.get("espn_s2") or "").strip()
        cleaned["swid"] = (cleaned.get("swid") or "").strip()
        cleaned["name"] = (cleaned.get("name") or "").strip()
        is_private = bool(cleaned.get("is_private"))

        if platform != League.PLATFORM_ESPN or not is_private:
            cleaned["espn_s2"] = ""
            cleaned["swid"] = ""
        elif not cleaned.get("espn_s2") or not cleaned.get("swid"):
            raise ValidationError(
                "Private ESPN leagues need both the espn_s2 and SWID cookies."
            )

        if self.user and platform and league_id:
            duplicate = League.objects.filter(
                user=self.user,
                platform=platform,
                league_id=league_id,
            )
            if self.instance.pk:
                duplicate = duplicate.exclude(pk=self.instance.pk)
            if duplicate.exists():
                raise ValidationError("You already added that league.")

        if platform and league_id and not self.errors:
            try:
                remote_name = lookup_league(
                    platform=platform,
                    league_id=league_id,
                    espn_s2=cleaned.get("espn_s2") or "",
                    swid=cleaned.get("swid") or "",
                )
            except LeagueLookupError as exc:
                raise ValidationError(str(exc)) from exc
            if not (cleaned.get("name") or "").strip():
                cleaned["name"] = remote_name

        return cleaned
