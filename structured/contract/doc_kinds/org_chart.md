recognise:
  path_globs: ["**/*org*chart*"]
  any_phrases: ["source document"]
  container: pdf

# org_chart

Extract grounded fields, preserve source-language evidence, and retain unresolved joins explicitly.

## Seats

An org chart yields one `org_seat` per seat, including seats without a person. A seat is a job title on the chart.
A unit, area, site or section heading is not a seat and is never a holder.

Subject, exactly these keys:
- `source_path`: the path of this document as given to you. Empty string if you were not given one; never invent it.
- `title`: the job title as printed, without the area or site.
- `scope`: the area or site printed with the title, empty string if none.

Write all five attributes for every seat, each as a string:
- `holder`: the person's name as printed, without words about their state. Empty when there is no person.
- `status`: exactly one of these words.
  - `held`: the seat names a person and nothing printed with the seat says they are not in it. A named person
    with a printed start date is `held`, also when that date is still to come: `valid_from` must then be that date.
  - `vacant`: vacancy wording in any language, in place of a name or next to it (for example "not filled",
    "open", "search ongoing"), or a seat with nothing printed for a person.
  - `absent`: leave, retiring, out of office or left the company, on the same line as the name or title or on a
    line next to them that belongs to the seat.
  - `unclear`: anything that is not clearly one of the three above. This includes words you cannot tell to be a
    person's name or a heading, a qualifier you do not understand, a person said not to have started with no date,
    a start that is not printed as a full date, and two seats with the same title and scope that name different
    people (write that seat once, with an empty holder).
- `valid_from`, `valid_until`: ISO dates (YYYY-MM-DD) if printed for the seat, else empty string.
- `line_key`: the `line_key` of the `x_document_line` the holder was read from. Empty string if you were not given
  one; never invent or compute it.

Capital letters and position do not make words a person. When in doubt the status is `unclear`, never `held`.
Give the exact printed text of the seat, with any wording about its state, as `evidence` of `holder` and `status`.
